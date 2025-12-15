import os
import time
from dataclasses import dataclass
from hashlib import sha1
from html.parser import HTMLParser
from typing import Any
import xml.etree.ElementTree as ET

import httpx
import toml
from sqlalchemy import Integer, String, Text, create_engine, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column


@dataclass(frozen=True)
class FeedConfig:
    id: str
    title: str
    url: str
    enabled: bool
    tags: list[str]


@dataclass(frozen=True)
class FeedItem:
    feed_id: str
    feed_title: str
    title: str
    link: str
    published: str


def derive_feed_id(url: str) -> str:
    return sha1(url.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class ParsedEntry:
    title: str
    link: str
    published: str
    entry_id: str


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data:
            self._parts.append(data)

    def get_text(self) -> str:
        return " ".join(p.strip() for p in self._parts if p.strip()).strip()


def _strip_html(value: str) -> str:
    v = (value or "").strip()
    if not v:
        return ""
    if "<" not in v and ">" not in v:
        return " ".join(v.split())
    parser = _HTMLTextExtractor()
    try:
        parser.feed(v)
        parser.close()
    except Exception:
        return " ".join(v.split())
    return " ".join(parser.get_text().split())


def _local_name(tag: str) -> str:
    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag


def _find_child(elem: ET.Element, name: str) -> ET.Element | None:
    for child in list(elem):
        if _local_name(child.tag) == name:
            return child
    return None


def _find_children(elem: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in list(elem) if _local_name(c.tag) == name]


def _text_of_child(elem: ET.Element, name: str) -> str:
    child = _find_child(elem, name)
    if child is None:
        return ""
    return (child.text or "").strip()


def _atom_entry_link(entry: ET.Element) -> str:
    links = _find_children(entry, "link")
    for link in links:
        rel = (link.attrib.get("rel") or "").strip()
        href = (link.attrib.get("href") or "").strip()
        if href and (rel == "alternate" or rel == ""):
            return href
    for link in links:
        href = (link.attrib.get("href") or "").strip()
        if href:
            return href
    return ""


def parse_feed_xml(xml_text: str) -> list[ParsedEntry]:
    root = ET.fromstring(xml_text)
    root_name = _local_name(root.tag)

    entries: list[ET.Element] = []
    if root_name == "rss":
        channel = _find_child(root, "channel")
        if channel is not None:
            entries = _find_children(channel, "item")
    elif root_name == "feed":
        entries = _find_children(root, "entry")
    else:
        for e in root.iter():
            if _local_name(e.tag) in {"item", "entry"}:
                entries.append(e)

    parsed: list[ParsedEntry] = []
    for e in entries:
        name = _local_name(e.tag)
        if name == "entry":
            title = _strip_html(_text_of_child(e, "title"))
            link = _atom_entry_link(e)
            published = _text_of_child(e, "published") or _text_of_child(e, "updated")
            entry_id = _text_of_child(e, "id")
            parsed.append(
                ParsedEntry(
                    title=title or "(no title)",
                    link=link,
                    published=published,
                    entry_id=entry_id,
                )
            )
            continue

        title = _strip_html(_text_of_child(e, "title"))
        link = _text_of_child(e, "link")
        published = _text_of_child(e, "pubDate")
        entry_id = _text_of_child(e, "guid")
        parsed.append(
            ParsedEntry(
                title=title or "(no title)",
                link=link,
                published=published,
                entry_id=entry_id,
            )
        )

    return parsed


def parse_feed_toml(data: dict[str, Any]) -> list[FeedConfig]:
    feeds_raw = data.get("feeds", [])
    feeds: list[FeedConfig] = []
    for f in feeds_raw:
        try:
            url = str(f["url"]).strip()
            if not url:
                continue

            enabled = bool(f.get("enabled", True))
            tags = f.get("tags", [])
            if tags is None:
                tags = []

            feed_id = str(f.get("id") or "").strip() or derive_feed_id(url)

            title = str(f.get("title") or "").strip()
            if not title:
                title = feed_id

            feeds.append(
                FeedConfig(
                    id=feed_id,
                    title=title,
                    url=url,
                    enabled=enabled,
                    tags=[str(t) for t in tags],
                )
            )
        except Exception:
            continue
    return [f for f in feeds if f.enabled]


def load_feed_toml(source: str | None = None) -> list[FeedConfig]:
    src = (source or os.environ.get("FEED_TOML", "feed.toml")).strip()
    if src.startswith("http://") or src.startswith("https://"):
        with httpx.Client(follow_redirects=True, timeout=20) as client:
            r = client.get(src)
        r.raise_for_status()
        data = toml.loads(r.text)
        return parse_feed_toml(data)

    with open(src, "r", encoding="utf-8") as f:
        data = toml.load(f)
    return parse_feed_toml(data)


def feed_toml_source(source: str | None = None) -> str:
    return (source or os.environ.get("FEED_TOML", "feed.toml")).strip()


def is_remote_source(source: str) -> bool:
    return source.startswith("http://") or source.startswith("https://")


def resolve_local_feed_toml_path(source: str | None = None) -> str:
    src = feed_toml_source(source)
    if is_remote_source(src):
        raise ValueError("Remote FEED_TOML is not writable")
    return os.path.abspath(src)


def read_feed_toml_text(source: str | None = None) -> tuple[str, str]:
    src = feed_toml_source(source)
    if is_remote_source(src):
        with httpx.Client(follow_redirects=True, timeout=20) as client:
            r = client.get(src)
        r.raise_for_status()
        return r.text, src

    path = resolve_local_feed_toml_path(src)
    with open(path, "r", encoding="utf-8") as f:
        return f.read(), path


def validate_feed_toml_text(text: str) -> None:
    data = toml.loads(text)
    if not isinstance(data, dict):
        raise ValueError("Invalid TOML")

    feeds = data.get("feeds")
    if feeds is None:
        return
    if not isinstance(feeds, list):
        raise ValueError("feeds must be a list")
    for idx, f in enumerate(feeds):
        if not isinstance(f, dict):
            raise ValueError(f"feeds[{idx}] must be a table")
        url = f.get("url")
        if url is None or not str(url).strip():
            raise ValueError(f"feeds[{idx}].url is required")


def save_feed_toml_text(text: str, source: str | None = None) -> str:
    path = resolve_local_feed_toml_path(source)
    validate_feed_toml_text(text)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def default_cache_db_path(db_path: str | None = None) -> str:
    src = (db_path or os.environ.get("RSS_CACHE_DB", "rss_cache.sqlite3")).strip()
    if os.path.isabs(src):
        return src
    base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "..", src)


class Base(DeclarativeBase):
    pass


class FeedRow(Base):
    __tablename__ = "feeds"

    feed_id: Mapped[str] = mapped_column(String, primary_key=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    etag: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_modified: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)


class ItemRow(Base):
    __tablename__ = "items"

    feed_id: Mapped[str] = mapped_column(String, primary_key=True)
    item_id: Mapped[str] = mapped_column(Text, primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    link: Mapped[str] = mapped_column(Text, nullable=False)
    published: Mapped[str] = mapped_column(Text, nullable=False)
    inserted_at: Mapped[int] = mapped_column(Integer, nullable=False, index=True)


class CacheDB:
    def __init__(self, db_path: str | None = None) -> None:
        self.path = default_cache_db_path(db_path)
        self.engine = create_engine(f"sqlite:///{self.path}")
        Base.metadata.create_all(self.engine)

    def close(self) -> None:
        try:
            self.engine.dispose()
        except Exception:
            pass

    def get_feed_meta(self, feed_id: str) -> tuple[str | None, str | None]:
        with Session(self.engine) as session:
            row = session.execute(
                select(FeedRow.etag, FeedRow.last_modified).where(
                    FeedRow.feed_id == feed_id
                )
            ).one_or_none()
            if row is None:
                return None, None
            return row[0], row[1]

    def upsert_feed_meta(
        self, feed_id: str, url: str, etag: str | None, last_modified: str | None
    ) -> None:
        stmt = (
            sqlite_insert(FeedRow)
            .values(
                feed_id=feed_id,
                url=url,
                etag=etag,
                last_modified=last_modified,
                updated_at=int(time.time()),
            )
            .on_conflict_do_update(
                index_elements=[FeedRow.feed_id],
                set_={
                    "url": url,
                    "etag": etag,
                    "last_modified": last_modified,
                    "updated_at": int(time.time()),
                },
            )
        )
        with Session(self.engine) as session:
            session.execute(stmt)
            session.commit()

    def read_items(self, feed: FeedConfig, limit: int = 100) -> list[FeedItem]:
        with Session(self.engine) as session:
            rows = session.execute(
                select(ItemRow.title, ItemRow.link, ItemRow.published)
                .where(ItemRow.feed_id == feed.id)
                .order_by(ItemRow.inserted_at.desc())
                .limit(limit)
            ).all()

        return [
            FeedItem(
                feed_id=feed.id,
                feed_title=feed.title,
                title=str(r[0]),
                link=str(r[1]),
                published=str(r[2]),
            )
            for r in rows
        ]

    def upsert_items(self, feed: FeedConfig, entries: list[Any]) -> int:
        now = int(time.time())
        inserted = 0

        with Session(self.engine) as session:
            for e in entries:
                title = str(getattr(e, "title", "(no title)") or "(no title)")
                link = str(getattr(e, "link", "") or "")
                published = str(getattr(e, "published", "") or "")
                item_id = entry_item_id(e)

                stmt = (
                    sqlite_insert(ItemRow)
                    .values(
                        feed_id=feed.id,
                        item_id=item_id,
                        title=title,
                        link=link,
                        published=published,
                        inserted_at=now,
                    )
                    .on_conflict_do_nothing(
                        index_elements=[ItemRow.feed_id, ItemRow.item_id]
                    )
                )
                res = session.execute(stmt)
                if res.rowcount:
                    inserted += int(res.rowcount)

            session.commit()

        return inserted


def entry_item_id(entry: Any) -> str:
    if isinstance(entry, ParsedEntry):
        if entry.entry_id:
            return entry.entry_id
        if entry.link:
            return entry.link
        return sha1(f"{entry.title}\n{entry.published}".encode("utf-8")).hexdigest()

    guid = getattr(entry, "id", None) or getattr(entry, "guid", None)
    if guid:
        return str(guid)
    link = getattr(entry, "link", None)
    if link:
        return str(link)
    title = str(getattr(entry, "title", "") or "")
    published = str(getattr(entry, "published", "") or "")
    return sha1(f"{title}\n{published}".encode("utf-8")).hexdigest()


def fetch_feed_items(cache: CacheDB, feed: FeedConfig) -> tuple[list[FeedItem], str]:
    etag, last_modified = cache.get_feed_meta(feed.id)
    headers: dict[str, str] = {"User-Agent": "flet-rss-feed/0.1"}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    try:
        with httpx.Client(follow_redirects=True, timeout=20) as client:
            r = client.get(feed.url, headers=headers)
    except Exception as e:
        cached = cache.read_items(feed)
        return cached, f"Network error, showing cached items: {e}"

    if r.status_code == 304:
        cached = cache.read_items(feed)
        return cached, "Not modified (304), loaded from cache."

    if r.status_code < 200 or r.status_code >= 300:
        cached = cache.read_items(feed)
        return cached, f"HTTP {r.status_code}, showing cached items."

    try:
        parsed_entries = parse_feed_xml(r.text)
    except Exception as e:
        cached = cache.read_items(feed)
        return cached, f"Failed to parse feed XML, showing cached items: {e}"

    inserted = cache.upsert_items(feed, parsed_entries[:200])

    new_etag = r.headers.get("etag") or r.headers.get("ETag")
    new_last_modified = r.headers.get("last-modified") or r.headers.get("Last-Modified")
    cache.upsert_feed_meta(feed.id, feed.url, new_etag, new_last_modified)

    items = cache.read_items(feed)
    return items, f"Updated. New items: {inserted}."
