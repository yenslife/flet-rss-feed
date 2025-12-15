from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import os
import threading

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
    page.title = "flet-rss-feed"
    page.window_width = 1280
    page.window_height = 800
    page.window_min_width = 380
    page.window_min_height = 640
    page.theme_mode = ft.ThemeMode.SYSTEM
    page.adaptive = True

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
        expand=True,
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
                    title=ft.Text(it.title, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS),
                    subtitle=ft.Text(subtitle, size=12, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
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
            item_list.controls.append(ft.Text("目前沒有文章。"))

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
            status.value = f"已讀取快取文章：{min(display_limit, len(items))}/{len(items)} 篇。 ({_now_text()})"
        except Exception as e:
            all_items = []
            status.value = f"讀取快取失敗：{e}"

        render_items_with_pagination()

    def open_feed_editor(_: ft.ControlEvent) -> None:
        src = feed_toml_source()
        remote = is_remote_source(src)

        try:
            current_text, src_label = read_feed_toml_text(src)
        except Exception as e:
            page.open(
                ft.AlertDialog(
                    title=ft.Text("錯誤"),
                    content=ft.Text(f"讀取訂閱來源失敗：{e}"),
                )
            )
            return

        # Components
        editor = ft.TextField(
            value=current_text,
            multiline=True,
            min_lines=5,  # Allows it to shrink if needed
            expand=True,
            text_style=ft.TextStyle(font_family="monospace", size=13),
            bgcolor=getattr(ft.Colors, "SURFACE_VARIANT", ft.Colors.SURFACE),
            border_width=0,
            border_radius=8,
            content_padding=15,
        )

        error_text = ft.Text(value="", size=12)
        info_text = ft.Text(
            value=f"{src_label}" + (" (唯讀)" if remote else ""),
            size=12,
            color=ft.Colors.OUTLINE,
            overflow=ft.TextOverflow.ELLIPSIS,
            expand=True,
        )

        def close_overlay(_=None):
            if modal_container in page.overlay:
                page.overlay.remove(modal_container)
            if update_layout in resize_subscriptions:
                resize_subscriptions.remove(update_layout)
            page.update()

        def do_validate(_):
            try:
                validate_feed_toml_text(editor.value or "")
                error_text.value = "設定驗證成功。"
                error_text.color = ft.Colors.GREEN
            except Exception as e:
                error_text.value = str(e)
                error_text.color = ft.Colors.RED
            page.update()

        def do_save(_):
            if remote:
                return
            try:
                saved_path = save_feed_toml_text(editor.value or "", src)
                error_text.value = f"已儲存：{os.path.abspath(saved_path)}"
                error_text.color = ft.Colors.GREEN
                page.update()
                
                # Close after short delay or immediately? Let's close immediately for responsiveness
                # But user might want to see success message. Let's just reload data.
                load_subscriptions()
                show_cached_articles()
                close_overlay()
            except Exception as e:
                error_text.value = str(e)
                error_text.color = ft.Colors.RED
                page.update()

        # Actions
        actions_row = ft.Row(
            controls=[
                ft.TextButton("取消", on_click=close_overlay),
                ft.OutlinedButton("驗證設定", on_click=do_validate),
            ],
            alignment=ft.MainAxisAlignment.END,
        )
        if not remote:
            actions_row.controls.append(ft.FilledButton("儲存設定", on_click=do_save))

        # Main Card Layout
        card_content = ft.Column(
            controls=[
                # Header
                ft.Row(
                    [
                        ft.Text("編輯訂閱來源 (feed.toml)", size=18, weight=ft.FontWeight.BOLD),
                        ft.IconButton(ft.Icons.CLOSE, on_click=close_overlay),
                    ],
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                ),
                # Info Bar
                ft.Row(
                    [
                        ft.Icon(ft.Icons.DESCRIPTION, size=14, color=ft.Colors.OUTLINE),
                        info_text,
                    ],
                    alignment=ft.MainAxisAlignment.START,
                    spacing=5,
                ),
                # Editor Area (This expands to fill available space)
                ft.Container(
                    content=editor,
                    expand=True,  # Critical for filling the middle space
                    padding=ft.padding.symmetric(vertical=10),
                ),
                # Status & Footer
                ft.Row(
                    [
                        ft.Container(content=error_text, expand=True),
                    ]
                ),
                actions_row,
            ],
            expand=True, # Critical: The Column must fill the Container
            spacing=5,
        )

        modal_card = ft.Container(
            content=card_content,
            bgcolor=ft.Colors.SURFACE,
            padding=20,
            border_radius=12,
            shadow=ft.BoxShadow(
                blur_radius=15, spread_radius=1, color=ft.Colors.with_opacity(0.5, ft.Colors.BLACK)
            ),
            # Size will be set by update_layout
        )

        # Backdrop
        modal_container = ft.Container(
            content=modal_card,
            bgcolor=ft.Colors.with_opacity(0.5, ft.Colors.BLACK),
            alignment=ft.alignment.center,
            on_click=lambda e: None, # Consume clicks on backdrop if desired, or close_overlay to close on click outside
            # We'll make only the backdrop clickable to close? 
            # Actually, standard behavior is clicking backdrop closes.
            # But let's keep it safe for code editing, require explicit cancel.
            expand=True,
        )

        # Responsive Layout Logic
        def update_layout(_=None):
            w = page.width or 800
            h = page.height or 600
            
            if w < 768:
                # Mobile: Full screen ish
                card_w = w * 0.95
                card_h = h * 0.90
            else:
                # Desktop: Large centered modal
                card_w = min(900, w * 0.85)
                card_h = min(700, h * 0.85)

            modal_card.width = card_w
            modal_card.height = card_h
            if modal_card.page:
                modal_card.update()

        resize_subscriptions.append(update_layout)

        # Show it
        page.overlay.append(modal_container)
        page.update()
        update_layout() # Initial sizing

    def load_subscriptions() -> None:
        nonlocal feeds, selected_feed
        nonlocal display_limit
        progress.visible = True
        status.value = f"正在讀取 feed.toml 設定檔... ({_now_text()})"
        page.update()

        try:
            feeds = load_feed_toml()
            if selected_feed is None and feeds:
                selected_feed = feeds[0]
            display_limit = PAGE_SIZE
            status.value = f"已載入 {len(feeds)} 個訂閱來源。 ({_now_text()})"
        except Exception as e:
            feeds = []
            selected_feed = None
            status.value = f"讀取 feed.toml 失敗：{e}"
        finally:
            progress.visible = False
            render_feed_list()
            set_items([])
            page.update()

    def render_feed_list() -> None:
        feed_list.controls.clear()

        if not feeds:
            feed_list.controls.append(ft.Text("未找到任何訂閱。"))
            return

        for f in feeds:
            is_selected = selected_feed is not None and f.id == selected_feed.id

            def on_click(_: ft.ControlEvent, feed_id: str = f.id) -> None:
                select_feed(feed_id)

            feed_list.controls.append(
                ft.ListTile(
                    title=ft.Text(
                        f.title, 
                        weight=ft.FontWeight.BOLD if is_selected else None,
                        max_lines=1,
                        overflow=ft.TextOverflow.ELLIPSIS
                    ),
                    subtitle=ft.Text(f.url, size=12, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                    on_click=on_click,
                    selected=is_selected,
                )
            )

    def select_feed(feed_id: str) -> None:
        nonlocal selected_feed
        nonlocal display_limit
        selected_feed = next((f for f in feeds if f.id == feed_id), None)
        
        if current_layout_mode == "mobile":
            page.close(drawer)

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
        status.value = f"正在更新：{selected_feed.title}... ({_now_text()})"
        page.update()

        try:
            items, msg = fetch_feed_items(cache, selected_feed)
            items = apply_sort_and_filter(items)
            set_items(items)
            status.value = f"{msg} 目前顯示：{min(display_limit, len(items))}/{len(items)} 篇。 ({_now_text()})"
        except Exception as e:
            status.value = f"更新失敗：{e}"
            set_items([])
        finally:
            progress.visible = False
            page.update()

    def crawl_all_feeds(_: ft.ControlEvent) -> None:
        if not feeds:
            return

        # Disable buttons to prevent re-entry
        btn_crawl_this.disabled = True
        btn_crawl_all.disabled = True
        progress.visible = True
        status.value = f"開始更新全部訂閱... ({_now_text()})"
        page.update()

        def _task():
            # Create a dedicated independent cache connection for this thread
            # to avoid interference with the main thread's loop or SQLite lock issues
            local_cache = CacheDB()
            
            count = len(feeds)
            processed = 0
            
            for i, f in enumerate(feeds):
                msg_prefix = f"[{i+1}/{count}] {f.title}"
                status.value = f"{msg_prefix}: 正在更新..."
                page.update()
                
                try:
                    # We assume fetch_feed_items updates the DB side-effect
                    fetch_feed_items(local_cache, f)
                except Exception as e:
                    print(f"Failed {f.title}: {e}")
                
                processed += 1
            
            local_cache.close()
            
            status.value = f"全部更新完成。共處理 {processed}/{count} 個訂閱源。 ({_now_text()})"
            
            # Refresh current view
            show_cached_articles()
            
            # Re-enable buttons
            btn_crawl_this.disabled = False
            btn_crawl_all.disabled = False
            progress.visible = False
            page.update()

        threading.Thread(target=_task, daemon=True).start()

    # Toolbar Buttons
    btn_edit = ft.ElevatedButton(text="編輯訂閱源", on_click=open_feed_editor)
    btn_crawl_this = ft.ElevatedButton(text="更新此訂閱", on_click=lambda e: crawl_feed())
    btn_crawl_all = ft.ElevatedButton(text="更新全部", on_click=crawl_all_feeds)

    # Drawer for Mobile
    drawer = ft.NavigationDrawer(controls=[])
    page.drawer = drawer

    def toggle_drawer(_):
        page.open(drawer)

    menu_button = ft.IconButton(ft.Icons.MENU, on_click=toggle_drawer, visible=False)

    toolbar = ft.Row(
        controls=[
            menu_button,
            ft.Container(
                expand=True, # Push buttons to the right
            ),
            ft.Row(
                controls=[
                    btn_edit,
                    btn_crawl_this,
                    btn_crawl_all,
                ],
                alignment=ft.MainAxisAlignment.END,
            ),
        ],
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
    )

    feeds_col = ft.Column(
        controls=[
            ft.Text("訂閱列表", weight=ft.FontWeight.BOLD),
            feed_list,
        ],
        expand=True,
    )

    feeds_panel = ft.Container(
        content=feeds_col,
        expand=True,
        padding=8,
        border=ft.border.all(1, outline_color),
        border_radius=8,
    )

    items_panel = ft.Container(
        expand=True,
        content=ft.Column(
            controls=[
                ft.ResponsiveRow(
                    controls=[
                        ft.Container(
                            content=ft.Text("文章列表", weight=ft.FontWeight.BOLD),
                            col={"xs": 12, "sm": 3, "md": 2},
                            alignment=ft.alignment.center_left,
                            padding=ft.padding.only(top=5),
                        ),
                        ft.Container(
                            content=search_field, col={"xs": 12, "sm": 9, "md": 10}
                        ),
                    ],
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                item_list,
            ],
            expand=True,
        ),
        padding=8,
        border=ft.border.all(1, outline_color),
        border_radius=8,
    )

    content_container = ft.Container(expand=True)
    current_layout_mode = None
    resize_subscriptions = []

    def apply_responsive_sizing() -> None:
        nonlocal current_layout_mode
        width = page.width or 0

        # Smart RWD Strategy:
        # < 950px: Mobile Mode. Use Drawer for Feeds to maximize content area.
        # >= 950px: Desktop Mode. Use Fixed Sidebar for Feeds (stable readability).
        if width < 950:
            mode = "mobile"
        else:
            mode = "desktop"

        if mode == current_layout_mode:
            return

        current_layout_mode = mode

        # Helper to ensure feed_list is in the right place
        def reparent_feed_list(target_controls_list):
            # Remove from everywhere else
            if feed_list in drawer.controls:
                drawer.controls.remove(feed_list)
            if feed_list in feeds_col.controls:
                feeds_col.controls.remove(feed_list)
            
            # Add to target
            if feed_list not in target_controls_list:
                target_controls_list.append(feed_list)

        if mode == "mobile":
            # Mobile View: Sidebar hidden, Feeds in Drawer
            menu_button.visible = True
            
            # Setup Drawer Content
            drawer.controls.clear()
            drawer.controls.extend([
                ft.Container(
                    content=ft.Text("訂閱列表", size=20, weight=ft.FontWeight.BOLD),
                    padding=ft.padding.only(left=20, top=20, bottom=10)
                ),
                ft.Divider(),
                feed_list # This implicitly reparents in Flet runtime usually, but we manage it explicitly generally
            ])
            
            # Explicit Reparenting logic to be safe
            if feed_list in feeds_col.controls:
                feeds_col.controls.remove(feed_list)
            
            feed_list.expand = True 
            
            # Main layout only shows items
            content_container.content = items_panel
        
        else:
            # Desktop View: Fixed Sidebar
            menu_button.visible = False
            
            # Move feed_list back to sidebar
            if feed_list in drawer.controls:
                drawer.controls.remove(feed_list)
            if feed_list not in feeds_col.controls:
                feeds_col.controls.append(feed_list)

            feed_list.expand = True

            # Use Fixed Width for Sidebar on Desktop
            # This prevents the "squashed" look on intermediate screen sizes
            feeds_panel.width = 300
            feeds_panel.expand = False
            
            items_panel.width = None
            items_panel.expand = True
            
            content_container.content = ft.Row(
                controls=[feeds_panel, items_panel],
                expand=True,
                spacing=10,
            )

        page.update()

    layout = ft.Column(
        controls=[
            toolbar,
            progress,
            ft.Divider(height=1),
            content_container,
            ft.Divider(height=1),
            status,
        ],
        expand=True,
    )

    # Wrap layout in SafeArea to avoid system UI overlap on mobile
    safe_layout = ft.SafeArea(layout, expand=True)

    page.add(safe_layout)

    def on_resize_handler(e):
        apply_responsive_sizing()
        for sub in list(resize_subscriptions):  # Copy list to safe iterate
            try:
                sub(e)
            except Exception:
                pass

    page.on_resize = on_resize_handler
    apply_responsive_sizing()
    load_subscriptions()
    show_cached_articles()

    def on_close(_: ft.ControlEvent) -> None:
        cache.close()

    page.on_close = on_close
