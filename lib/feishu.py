"""Feishu document fetcher with virtual scroll collection."""
import base64
import os
import re
import time

from lib.router import check_dependency


def fetch_feishu(url, output_dir, no_images=False):
    """Fetch Feishu doc with virtual scroll collection. Returns output path."""
    if not check_dependency("scrapling"):
        return None
    if not check_dependency("html2text"):
        return None

    from scrapling import StealthyFetcher
    import html2text

    os.makedirs(output_dir, exist_ok=True)

    print(f"[*] Feishu fetch: {url}")

    collected_html = []
    collected_img_urls = []

    def scroll_and_collect(page):
        """Scroll through Feishu doc and collect all content blocks."""
        nonlocal collected_html, collected_img_urls

        # Wait for content to load
        page.wait_for_selector("[data-content-editable-root]", timeout=30000)
        time.sleep(2)

        # Inject collector
        page.evaluate("""
            window.__feishu_collected = new Map();
            window.__feishu_collect = function() {
                document.querySelectorAll('[data-block-id]').forEach(el => {
                    const id = el.getAttribute('data-block-id');
                    if (!window.__feishu_collected.has(id)) {
                        window.__feishu_collected.set(id, el.outerHTML);
                    }
                });
                return window.__feishu_collected.size;
            };
        """)

        # Find scroll container
        container = page.query_selector(".bear-web-x-container")
        if not container:
            container = page.query_selector("[data-content-editable-root]")
        if not container:
            print("[!] Could not find scroll container")
            return

        stable_count = 0
        last_count = 0

        for iteration in range(200):
            # Collect visible blocks
            count = page.evaluate("window.__feishu_collect()")

            # Scroll down
            page.evaluate("""
                (container) => {
                    const el = document.querySelector('.bear-web-x-container') ||
                               document.querySelector('[data-content-editable-root]');
                    if (el) el.scrollTop += 800;
                }
            """)
            time.sleep(0.3)

            if count == last_count:
                stable_count += 1
                if stable_count >= 15:
                    print(f"[*] Collection stabilized at {count} blocks")
                    break
            else:
                stable_count = 0
            last_count = count

        # Extract collected HTML
        fragments = page.evaluate("Array.from(window.__feishu_collected.values())")
        collected_html.extend(fragments)

        # Collect image URLs if needed
        if not no_images:
            img_srcs = page.evaluate("""
                Array.from(document.querySelectorAll('img[src]')).map(img => img.src)
                .filter(src => src.startsWith('http') && !src.includes('data:'))
            """)
            collected_img_urls.extend(img_srcs)

            # Download images via browser fetch (cookies needed for 401)
            for i, img_url in enumerate(img_srcs):
                try:
                    b64_data = page.evaluate("""
                        async (url) => {
                            try {
                                const resp = await fetch(url, {credentials: 'include'});
                                const blob = await resp.blob();
                                return new Promise((resolve) => {
                                    const reader = new FileReader();
                                    reader.onloadend = () => resolve(reader.result);
                                    reader.readAsDataURL(blob);
                                });
                            } catch(e) { return null; }
                        }
                    """, img_url)
                    if b64_data:
                        collected_img_urls[i] = ("local", img_url, b64_data)
                except Exception:
                    pass

    try:
        response = StealthyFetcher.fetch(
            url,
            headless=True,
            network_idle=True,
            page_action=scroll_and_collect,
        )
    except Exception as e:
        print(f"[!] StealthyFetcher error: {e}")
        return None

    if not collected_html:
        print("[!] No content collected")
        return None

    # Join HTML fragments
    full_html = "\n".join(collected_html)

    # Convert to markdown
    h = html2text.HTML2Text()
    h.ignore_links = False
    h.ignore_images = False
    h.body_width = 0
    md_text = h.handle(full_html)

    # Clean up "Unable to print" artifacts
    md_text = re.sub(r'Unable to print\s*', '', md_text)

    # Extract title
    title = None
    for line in md_text.split("\n"):
        line = line.strip()
        if line.startswith("#"):
            title = re.sub(r'^#+\s*', '', line).strip()
            break
        if line and len(line) > 2:
            title = line[:80]
            break
    slug = _slugify(title) if title else "feishu-doc"

    # Save images
    img_dir = os.path.join(output_dir, "images", slug)
    if not no_images and collected_img_urls:
        os.makedirs(img_dir, exist_ok=True)
        for i, item in enumerate(collected_img_urls):
            if isinstance(item, tuple) and item[0] == "local":
                _, orig_url, b64_data = item
                # Parse data URL
                match = re.match(r'data:image/(\w+);base64,(.+)', b64_data)
                if match:
                    ext = match.group(1)
                    if ext == "jpeg":
                        ext = "jpg"
                    img_data = base64.b64decode(match.group(2))
                    local_name = f"img_{i:02d}.{ext}"
                    local_path = os.path.join(img_dir, local_name)
                    with open(local_path, "wb") as f:
                        f.write(img_data)
                    local_ref = os.path.join("images", slug, local_name)
                    md_text = md_text.replace(orig_url, local_ref)

    md_path = os.path.join(output_dir, f"{slug}.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_text)

    print(f"[+] Saved: {md_path}")
    return md_path


def _slugify(title):
    """Generate filesystem-safe slug from article title."""
    slug = re.sub(r'[^\w\s\u4e00-\u9fff-]', '', title)
    slug = re.sub(r'\s+', '-', slug.strip())
    return slug[:100] if slug else "feishu-doc"
