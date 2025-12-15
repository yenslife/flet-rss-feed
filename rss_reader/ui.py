from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import os

import flet as ft

from .core import (
    CacheDB,
    FeedConfig,
    FeedItem,
    fetch_feed_items,
    feed_toml_source,
    is_remote_source,
    load_feed_toml,
    read_feed_toml_text,
    save_feed_toml_text,
    validate_feed_toml_text,
)


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def run_app() -> None:
    ft.app(target=_app)


def _app(page: ft.Page) -> None:
    page.title = "yenslife-rss-feed"
    page.window_width = 1280
    page.window_height = 800
    page.window_min_width = 1150
    page.window_min_height = 700
    page.theme_mode = ft.ThemeMode.SYSTEM

    outline_color = getattr(ft.Colors, "OUTLINE_VARIANT", ft.Colors.OUTLINE)

    cache = CacheDB()

    feeds: list[FeedConfig] = []
    selected_feed: FeedConfig | None = None

    status = ft.Text(value="")
    progress = ft.ProgressBar(visible=False)

    feed_list = ft.ListView(expand=True, spacing=4, padding=8, auto_scroll=False)
    item_list = ft.ListView(expand=True, spacing=6, padding=8, auto_scroll=False)

    PAGE_SIZE = 20
    current_query = ""
    display_limit = PAGE_SIZE
    all_items: list[FeedItem] = []

    def _try_parse_published_ts(value: str) -> float | None:
        v = (value or "").strip()
        if not v:
            return None

        try:
            dt = parsedate_to_datetime(v)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            pass

        try:
            dt = datetime.fromisoformat(v)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            return None

    def sort_items_newest_first(items: list[FeedItem]) -> list[FeedItem]:
        keyed: list[tuple[int, float, int, FeedItem]] = []
        for idx, it in enumerate(items):
            ts = _try_parse_published_ts(it.published)
            missing = 1 if ts is None else 0
            keyed.append((missing, float(ts or 0.0), idx, it))

        keyed.sort(key=lambda x: (x[0], -x[1], x[2]))
        return [x[3] for x in keyed]

    def filter_items(items: list[FeedItem]) -> list[FeedItem]:
        q = current_query.strip().casefold()
        if not q:
            return items

        out: list[FeedItem] = []
        for it in items:
            hay = "\n".join(
                [
                    it.title or "",
                    it.link or "",
                    it.published or "",
                ]
            ).casefold()
            if q in hay:
                out.append(it)
        return out

    def apply_sort_and_filter(items: list[FeedItem]) -> list[FeedItem]:
        return filter_items(sort_items_newest_first(items))

    def on_query_change(e: ft.ControlEvent) -> None:
        nonlocal current_query
        nonlocal display_limit
        current_query = str(e.control.value or "")
        display_limit = PAGE_SIZE
        show_cached_articles()

    search_field = ft.TextField(
        value=current_query,
        hint_text="搜尋（標題/連結/時間）",
        dense=True,
        width=280,
        on_change=on_query_change,
    )

    def render_items_with_pagination() -> None:
        item_list.controls.clear()

        shown = all_items[:display_limit]
        for it in shown:

            def open_link(_: ft.ControlEvent, url: str = it.link) -> None:
                if url:
                    page.launch_url(url)

            subtitle = it.published.strip() if it.published else it.feed_title

            item_list.controls.append(
                ft.ListTile(
                    title=ft.Text(it.title),
                    subtitle=ft.Text(subtitle, size=12),
                    on_click=open_link,
                )
            )

        if display_limit < len(all_items):
            item_list.controls.append(
                ft.Container(
                    content=ft.ElevatedButton(
                        text="顯示更多", on_click=lambda e: load_more()
                    ),
                    padding=12,
                    alignment=ft.alignment.center,
                )
            )
        elif not all_items:
            item_list.controls.append(ft.Text("No items."))

        page.update()

    def set_items(items: list[FeedItem]) -> None:
        nonlocal all_items
        all_items = items
        render_items_with_pagination()

    def load_more() -> None:
        nonlocal display_limit
        display_limit = min(display_limit + PAGE_SIZE, len(all_items))
        render_items_with_pagination()

    def show_cached_articles() -> None:
        nonlocal all_items
        if selected_feed is None:
            all_items = []
            render_items_with_pagination()
            return

        try:
            items = cache.read_items(selected_feed)
            items = apply_sort_and_filter(items)
            all_items = items
            status.value = f"Loaded cached items: {min(display_limit, len(items))}/{len(items)}. ({_now_text()})"
        except Exception as e:
            all_items = []
            status.value = f"Failed to load cached items: {e}"

        render_items_with_pagination()

    def open_feed_editor(_: ft.ControlEvent) -> None:
        src = feed_toml_source()
        remote = is_remote_source(src)

        try:
            current_text, src_label = read_feed_toml_text(src)
        except Exception as e:
            current_text = ""
            src_label = src

            dlg = ft.AlertDialog(
                modal=True,
                title=ft.Text("Edit feed.toml"),
                content=ft.Text(f"Failed to read feed source: {e}"),
                actions=[ft.TextButton("OK", on_click=lambda ev: page.close(dlg))],
            )
            page.open(dlg)
            return

        editor = ft.TextField(
            value=current_text,
            multiline=True,
            min_lines=20,
            max_lines=30,
            expand=True,
        )
        error_text = ft.Text(value="", color=ft.Colors.RED)
        info_text = ft.Text(value=f"Source: {src_label}", size=12)

        def do_validate(_: ft.ControlEvent) -> None:
            try:
                validate_feed_toml_text(editor.value or "")
                error_text.value = "OK"
                error_text.color = ft.Colors.GREEN
            except Exception as e:
                error_text.value = str(e)
                error_text.color = ft.Colors.RED
            page.update()

        def do_save(_: ft.ControlEvent) -> None:
            if remote:
                error_text.value = "Remote FEED_TOML is not writable."
                error_text.color = ft.Colors.RED
                page.update()
                return

            try:
                saved_path = save_feed_toml_text(editor.value or "", src)
                error_text.value = f"Saved: {os.path.abspath(saved_path)}"
                error_text.color = ft.Colors.GREEN
                page.update()
                page.close(dlg)
                load_subscriptions()
                show_cached_articles()
            except Exception as e:
                error_text.value = str(e)
                error_text.color = ft.Colors.RED
                page.update()

        actions = [
            ft.TextButton("Validate", on_click=do_validate),
            ft.TextButton("Cancel", on_click=lambda ev: page.close(dlg)),
        ]
        if not remote:
            actions.insert(1, ft.ElevatedButton("Save", on_click=do_save))

        if remote:
            info_text.value = f"Source: {src_label} (read-only)"

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("Edit feed.toml"),
            content=ft.Column(
                controls=[
                    info_text,
                    editor,
                    error_text,
                ],
                tight=True,
                width=800,
                height=520,
            ),
            actions=actions,
            actions_alignment=ft.MainAxisAlignment.END,
        )
        page.open(dlg)

    def load_subscriptions() -> None:
        nonlocal feeds, selected_feed
        nonlocal display_limit
        progress.visible = True
        status.value = f"Loading feed.toml... ({_now_text()})"
        page.update()

        try:
            feeds = load_feed_toml()
            if selected_feed is None and feeds:
                selected_feed = feeds[0]
            display_limit = PAGE_SIZE
            status.value = f"Loaded {len(feeds)} feeds. ({_now_text()})"
        except Exception as e:
            feeds = []
            selected_feed = None
            status.value = f"Failed to load feed.toml: {e}"
        finally:
            progress.visible = False
            render_feed_list()
            set_items([])
            page.update()

    def render_feed_list() -> None:
        feed_list.controls.clear()

        if not feeds:
            feed_list.controls.append(ft.Text("No feeds found."))
            return

        for f in feeds:
            is_selected = selected_feed is not None and f.id == selected_feed.id

            def on_click(_: ft.ControlEvent, feed_id: str = f.id) -> None:
                select_feed(feed_id)

            feed_list.controls.append(
                ft.ListTile(
                    title=ft.Text(
                        f.title, weight=ft.FontWeight.BOLD if is_selected else None
                    ),
                    subtitle=ft.Text(f.url, size=12),
                    on_click=on_click,
                    selected=is_selected,
                )
            )

    def select_feed(feed_id: str) -> None:
        nonlocal selected_feed
        nonlocal display_limit
        selected_feed = next((f for f in feeds if f.id == feed_id), None)
        display_limit = PAGE_SIZE
        set_items([])
        render_feed_list()
        page.update()
        show_cached_articles()

    def crawl_feed() -> None:
        if selected_feed is None:
            set_items([])
            return

        progress.visible = True
        status.value = f"Fetching: {selected_feed.title}... ({_now_text()})"
        page.update()

        try:
            items, msg = fetch_feed_items(cache, selected_feed)
            items = apply_sort_and_filter(items)
            set_items(items)
            status.value = f"{msg} Items: {min(display_limit, len(items))}/{len(items)}. ({_now_text()})"
        except Exception as e:
            status.value = f"Failed to fetch feed: {e}"
            set_items([])
        finally:
            progress.visible = False
            page.update()

    toolbar = ft.Row(
        controls=[
            ft.Text("yenslife-rss-feed", size=16, weight=ft.FontWeight.BOLD),
            ft.Container(expand=True),
            ft.ElevatedButton(text="Edit feed.toml", on_click=open_feed_editor),
            ft.ElevatedButton(text="爬取 feed", on_click=lambda e: crawl_feed()),
        ],
        alignment=ft.MainAxisAlignment.START,
    )

    layout = ft.Column(
        controls=[
            toolbar,
            progress,
            ft.Divider(height=1),
            ft.Row(
                controls=[
                    ft.Container(
                        width=360,
                        content=ft.Column(
                            controls=[
                                ft.Text("Feeds", weight=ft.FontWeight.BOLD),
                                feed_list,
                            ],
                            expand=True,
                        ),
                        padding=8,
                        border=ft.border.all(1, outline_color),
                        border_radius=8,
                    ),
                    ft.Container(
                        expand=True,
                        content=ft.Column(
                            controls=[
                                ft.Row(
                                    controls=[
                                        ft.Text("Items", weight=ft.FontWeight.BOLD),
                                        ft.Container(expand=True),
                                        search_field,
                                    ],
                                ),
                                item_list,
                            ],
                            expand=True,
                        ),
                        padding=8,
                        border=ft.border.all(1, outline_color),
                        border_radius=8,
                    ),
                ],
                expand=True,
            ),
            ft.Divider(height=1),
            status,
        ],
        expand=True,
    )

    page.add(layout)
    load_subscriptions()
    show_cached_articles()

    def on_close(_: ft.ControlEvent) -> None:
        cache.close()

    page.on_close = on_close
