import asyncio
import os
import shutil
import json
import re
from urllib.parse import urlparse
from aiohttp import ClientSession, ClientTimeout, FormData
from urllib.parse import quote_plus
from playwright.async_api import async_playwright


async def upload_image_get_search_url(image_path: str, timeout: int = 20):
    """
    Uploads a local image to Google's upload endpoint and returns the resulting search URL
    (the location header or redirected URL). This avoids interacting with the UI file input.
    """
    upload_endpoint = "https://www.google.com/searchbyimage/upload"
    try:
        form = FormData()
        form.add_field('encoded_image', open(image_path, 'rb'))
        form.add_field('image_content', '')
        async with ClientSession(timeout=ClientTimeout(total=timeout)) as sess:
            async with sess.post(upload_endpoint, data=form, allow_redirects=False) as resp:
                # Google responds with a 302 redirect and a Location header pointing to the search results
                if resp.status in (302, 303) and 'Location' in resp.headers:
                    loc = resp.headers['Location']
                    # Location may be relative
                    if loc.startswith('/'):
                        return 'https://www.google.com' + loc
                    return loc
                # sometimes it returns HTML with a meta refresh; parse for /search?"
                text = await resp.text()
                # attempt to find /search?q= or /search?tbm=isch
                import re
                m = re.search(r'href="(/search\?[^\"]+)"', text)
                if m:
                    return 'https://www.google.com' + m.group(1)
    except Exception as e:
        print(f"upload_image_get_search_url failed: {e}")
    return None

"""
playwright_lens.py

Performs a Google Lens image search using Playwright. Supports either a local image file (uploaded) or an image URL.
Downloads visually similar image results into a folder and saves metadata as JSON.

Usage: edit the MAIN block at the bottom or import functions from this file.
"""


async def download_image(session, img_url, file_path, retries=3):
    attempt = 0
    while attempt < retries:
        try:
            async with session.get(img_url) as resp:
                if resp.status == 200:
                    with open(file_path, "wb") as f:
                        f.write(await resp.read())
                    print(f"Downloaded: {file_path}")
                    return True
                else:
                    print(f"Download failed {img_url} status {resp.status}")
        except Exception as e:
            print(f"Error downloading {img_url}: {e}")
        attempt += 1
        await asyncio.sleep(2 ** attempt)
    return False


async def search_with_lens(image_path=None, image_url=None, max_results=100, download_folder="lens_results", timeout=20):
    if not image_path and not image_url:
        raise ValueError("Provide either image_path or image_url")

    if os.path.exists(download_folder):
        # simple auto-clean for now
        shutil.rmtree(download_folder)
    os.makedirs(download_folder, exist_ok=True)

    json_out = os.path.join(download_folder, "results.json")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()
        # prepare network logging
        network_dir = os.path.join(download_folder, "network")
        os.makedirs(network_dir, exist_ok=True)
        network_index = 0
        # collect candidate image URLs discovered in XHR/async responses
        network_candidates = set()

        async def _on_response(resp):
            # capture JSON/XHR responses and any large JSON-like bodies for debugging
            nonlocal network_index
            try:
                url = resp.url
                ct = resp.headers.get('content-type', '')
                # only save JSON or javascript or plain text responses to limit noise
                if any(x in ct for x in ("application/json", "application/javascript", "text/plain", "text/html")) or ".json" in url:
                    body = await resp.text()
                    # don't save excessively large bodies
                    if body and len(body) < 200000:
                        fn = os.path.join(network_dir, f"resp_{network_index}.txt")
                        with open(fn, 'w', encoding='utf-8') as fh:
                            fh.write(f"URL: {url}\nCONTENT-TYPE: {ct}\n\n")
                            fh.write(body)
                        network_index += 1
                    # attempt to extract image URLs from the response body
                    try:
                        # JSON-style 'ou' keys often contain original image URLs in Google responses
                        for m in re.finditer(r'"ou"\s*:\s*"(https?://[^"]+)"', body):
                            u = m.group(1)
                            if u:
                                network_candidates.add(u)
                    except Exception:
                        pass
                    try:
                        # find http(s) urls and filter for likely image resources
                        urls = re.findall(r'https?://[^"\'\s>]+', body)
                        for u in urls:
                            u_clean = u.strip().rstrip('.,);')
                            if re.search(r'\.(?:jpg|jpeg|png|gif|webp|bmp|svg)(?:\?|$)', u_clean, re.I) or \
                               'gstatic' in u_clean or 'googleusercontent' in u_clean or 'encrypted-tbn' in u_clean:
                                network_candidates.add(u_clean)
                    except Exception:
                        pass
            except Exception:
                pass

        page.on('response', _on_response)
        # If a local file is provided, upload it directly to Google's upload endpoint
        # which returns a redirect to the search results page. This is more reliable
        # than interacting with the file input in the UI.
        if image_path:
            search_url = await upload_image_get_search_url(image_path)
            if not search_url:
                print("Upload endpoint did not return a search URL; falling back to UI flow")
                await page.goto("https://images.google.com/", timeout=60000)
            else:
                await page.goto(search_url, timeout=60000)
        else:
            # image_url path: construct a direct searchbyimage URL
            if image_url:
                u = f"https://www.google.com/searchbyimage?{ 'image_url=' + quote_plus(image_url) }"
                await page.goto(u, timeout=60000)
            else:
                await page.goto("https://images.google.com/", timeout=60000)

        # Click the camera (search by image) and open upload dialog. Try several selectors and give more time.
        try:
            # try a few ways to open the camera/upload dialog
            opened = False
            possible_selectors = [
                "div#qbi",
                "button[aria-label*='Search by image']",
                "g-flipper[aria-label*='Search by image']",
                "svg[aria-label*='Search by image']",
                "div[jscontroller='qrm8Od']",
            ]
            for sel in possible_selectors:
                try:
                    el = await page.query_selector(sel)
                    if el:
                        await el.click()
                        opened = True
                        break
                except Exception:
                    continue
            if not opened:
                # as a last resort, try clicking the camera icon by coordinates near the search box
                try:
                    await page.click('input[name=q]')
                    await asyncio.sleep(0.2)
                except Exception:
                    pass
        except Exception:
            pass

        await asyncio.sleep(1)

        # If image_url provided, use the 'Paste image URL' tab; otherwise use upload
        if image_url:
            # click 'Paste image URL' (button text might vary)
            try:
                # click the URL tab (if present)
                url_tab = await page.query_selector("#qbug a[href*='image_url'], button[aria-label*='Paste image URL']")
                if url_tab:
                    await url_tab.click()
            except Exception:
                pass

            # There may be an input to paste the URL into
            try:
                # fallback selector for URL input
                await page.fill("input[type='url']", image_url)
            except Exception:
                try:
                    await page.fill("input[aria-label*='Image URL']", image_url)
                except Exception:
                    # last resort: use clipboard paste via evaluate
                    await page.evaluate("(url) => { const el = document.querySelector('input[type=url]') || document.querySelector('input'); if(el){el.value=url; el.dispatchEvent(new Event('input', {bubbles:true}));} }", image_url)

            # submit if there's a button
            try:
                submit = await page.query_selector("button[type='submit']")
                if submit:
                    await submit.click()
            except Exception:
                pass

        else:
            # Upload local file: find file input and set files. Try multiple attempts and timeouts.
            upload_ok = False
            try:
                # First, try to open the Lens / camera upload modal by clicking the camera button
                # The debug HTML showed the camera button having jsname="R5mgy" and aria-label="Search by image"
                camera_selectors = [
                    "div[jsname=\"R5mgy\"]",
                    "div[aria-label=\"Search by image\"]",
                    "div[aria-label*='Search by image']",
                    "button[aria-label*='Search by image']",
                    "div.nrdQVe",
                    "div[data-base-lens-url]",
                ]

                async def find_file_input_in_frames():
                    # Check main page and all frames for an input[type=file]
                    try:
                        # top-level
                        el = await page.query_selector("input[type=file]")
                        if el:
                            return el
                    except Exception:
                        pass
                    # frames
                    for f in page.frames:
                        try:
                            el = await f.query_selector("input[type=file]")
                            if el:
                                return el
                        except Exception:
                            continue
                    return None

                clicked = False
                # Try multiple click attempts: click camera selectors and also click nearby container / search box
                for attempt_click in range(5):
                    for cs in camera_selectors:
                        try:
                            elc = await page.query_selector(cs)
                            if not elc:
                                continue
                            try:
                                await elc.click()
                                clicked = True
                                await asyncio.sleep(0.6)
                            except Exception:
                                try:
                                    # evaluate click in page context
                                    await page.evaluate("el => el.click()", elc)
                                    clicked = True
                                    await asyncio.sleep(0.6)
                                except Exception:
                                    continue
                        except Exception:
                            continue
                    # Also try clicking the general search box area (where camera icon lives)
                    try:
                        await page.click("input[name=q]")
                    except Exception:
                        pass

                    # After clicks, check if a file input is present in any frame
                    fi = await find_file_input_in_frames()
                    if fi:
                        upload_ok = True
                        # set the file on the frame's element
                        try:
                            await fi.set_input_files(image_path)
                        except Exception:
                            try:
                                await page.set_input_files("input[type=file]", image_path)
                            except Exception:
                                pass
                        break
                    await asyncio.sleep(0.8)

                # If we didn't manage to set files via frames search, leave upload_ok as False and continue to the original fallback
                for attempt in range(3):
                    try:
                        # wait longer for the file input which is inserted dynamically
                        await page.wait_for_selector("input[type=file]", timeout=10000)
                        file_input = await page.query_selector("input[type=file]")
                        if file_input:
                            await file_input.set_input_files(image_path)
                            upload_ok = True
                            break
                        else:
                            # try global helper
                            await page.set_input_files("input[type=file]", image_path)
                            upload_ok = True
                            break
                    except Exception:
                        await asyncio.sleep(1)
                        continue
            except Exception as e:
                print("Upload failed:", e)

            if not upload_ok:
                # Save debug artifacts and exit early
                debug_png = os.path.join(download_folder, "debug_upload_failed.png")
                debug_html = os.path.join(download_folder, "debug_upload_failed.html")
                try:
                    await page.screenshot(path=debug_png, full_page=True)
                except Exception:
                    pass
                try:
                    html = await page.content()
                    with open(debug_html, 'w', encoding='utf-8') as fh:
                        fh.write(html)
                except Exception:
                    pass
                print(f"Upload failed; saved debug artifacts to {download_folder}")
                await browser.close()
                return []

        # Wait for a results grid — Google uses #islrg for the image list
        try:
            await page.wait_for_selector("div#islrg", timeout=15000)
        except Exception:
            # if not present, still proceed
            pass

        await asyncio.sleep(1.5)

        # Try to load more thumbnails by scrolling so Google loads more results.
        # We'll scroll a few times until we have a buffer above the requested max_results.
        try:
            desired_thumbs = max(200, max_results * 3)
            for _ in range(15):
                try:
                    await page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
                except Exception:
                    pass
                await asyncio.sleep(1.0)
                try:
                    cnt = await page.evaluate("() => document.querySelectorAll('div#islrg img, img.rg_i, img.n3VNCb').length")
                except Exception:
                    cnt = 0
                # stop early if enough thumbnails loaded
                if cnt >= desired_thumbs:
                    break
        except Exception:
            pass

        # Collect candidate result images from typical result selectors
        selectors = ["img.rg_i", "img.n3VNCb", "div#islrg img"]
        candidate_urls = []
        for sel in selectors:
            try:
                elems = await page.query_selector_all(sel)
            except Exception:
                elems = []
            for el in elems:
                try:
                    src = await el.get_attribute('src') or ''
                    srcset = await el.get_attribute('srcset') or ''
                    data_src = await el.get_attribute('data-src') or ''
                    # parse srcset prefer largest
                    picked = None
                    if srcset:
                        parts = [p.strip() for p in srcset.split(',') if p.strip()]
                        best = None
                        best_w = 0
                        for p in parts:
                            seg = p.rsplit(' ', 1)
                            urlp = seg[0]
                            try:
                                w = int(seg[1][:-1]) if len(seg) > 1 and seg[1].endswith('w') else 0
                            except Exception:
                                w = 0
                            if w > best_w and urlp.startswith('http'):
                                best_w = w
                                best = urlp
                        if best:
                            picked = best
                    if not picked and data_src.startswith('http'):
                        picked = data_src
                    if not picked and src.startswith('http'):
                        picked = src
                    if picked and not picked.startswith('data:') and picked not in candidate_urls:
                        candidate_urls.append(picked)
                except Exception:
                    continue

        # If still nothing found, try to extract from links that wrap images
        if not candidate_urls:
            try:
                anchors = await page.query_selector_all('a[jsname]')
                for a in anchors:
                    try:
                        img = await a.query_selector('img')
                        if img:
                            src = await img.get_attribute('src') or ''
                            if src.startswith('http') and src not in candidate_urls:
                                candidate_urls.append(src)
                    except Exception:
                        continue
            except Exception:
                pass

        print(f"Collected {len(candidate_urls)} candidate image URLs from page (may include icons/logo etc)")

        # If we didn't find any candidate URLs, try opening thumbnails to extract preview images
        if not candidate_urls:
            try:
                # ensure results area is visible and scroll a bit
                try:
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight/4)")
                except Exception:
                    pass
                await asyncio.sleep(1)
                thumbs = await page.query_selector_all("div#islrg img, img.rg_i, img.n3VNCb")
                print(f"Fallback: found {len(thumbs)} thumbnail elements to try opening previews")
                preview_candidates = []
                # open up to a larger number of thumbnails to reach the requested max_results
                max_open = min(len(thumbs), max(200, max_results * 3))
                for idx, t in enumerate(thumbs[:max_open]):
                    try:
                        await t.click()
                        await asyncio.sleep(1.2)
                        # look for preview images typically in img.n3VNCb
                        full_imgs = await page.query_selector_all("img.n3VNCb, img.rg_i, img")
                        for fi in full_imgs:
                            try:
                                src = await fi.get_attribute('src') or ''
                                srcset = await fi.get_attribute('srcset') or ''
                                if srcset:
                                    parts = [p.strip() for p in srcset.split(',') if p.strip()]
                                    # pick largest
                                    best = None
                                    best_w = 0
                                    for p in parts:
                                        seg = p.rsplit(' ', 1)
                                        urlp = seg[0]
                                        try:
                                            w = int(seg[1][:-1]) if len(seg) > 1 and seg[1].endswith('w') else 0
                                        except Exception:
                                            w = 0
                                        if w > best_w and urlp.startswith('http'):
                                            best_w = w
                                            best = urlp
                                    if best and best not in preview_candidates:
                                        preview_candidates.append(best)
                                if src and src.startswith('http') and src not in preview_candidates:
                                    preview_candidates.append(src)
                            except Exception:
                                continue
                        # small pause between clicks
                        await asyncio.sleep(0.6)
                    except Exception:
                        continue
                # merge preview_candidates into candidate_urls
                for c in preview_candidates:
                    if c not in candidate_urls:
                        candidate_urls.append(c)
                print(f"After opening previews, collected {len(candidate_urls)} candidate URLs")
            except Exception as e:
                print("Fallback preview extraction failed:", e)

        # If still nothing, save debug artifacts to help diagnose
        if not candidate_urls:
            try:
                debug_png = os.path.join(download_folder, "debug_no_results.png")
                await page.screenshot(path=debug_png, full_page=True)
            except Exception:
                pass
            try:
                html = await page.content()
                debug_html = os.path.join(download_folder, "debug_no_results.html")
                with open(debug_html, 'w', encoding='utf-8') as fh:
                    fh.write(html)
            except Exception:
                pass
            # dump each frame's HTML for deeper inspection
            try:
                frames_dir = os.path.join(download_folder, "frames")
                os.makedirs(frames_dir, exist_ok=True)
                for i, f in enumerate(page.frames):
                    try:
                        fhtml = await f.content()
                        fp = os.path.join(frames_dir, f"frame_{i}.html")
                        with open(fp, 'w', encoding='utf-8') as fw:
                            fw.write(fhtml)
                    except Exception:
                        continue
            except Exception:
                pass
            print(f"No candidate URLs found; saved debug artifacts to {download_folder}")

        # Merge in any network-discovered candidates
        if network_candidates:
            try:
                netfile = os.path.join(download_folder, "network_candidates.txt")
                with open(netfile, 'w', encoding='utf-8') as nf:
                    for u in sorted(network_candidates):
                        nf.write(u + "\n")
                # add to candidate list, preserving order and uniqueness
                for u in sorted(network_candidates):
                    if u not in candidate_urls:
                        candidate_urls.append(u)
            except Exception:
                pass

        print(f"Collected {len(candidate_urls)} candidate image URLs from page or network (may include icons/logo etc)")

        # Download top N unique candidate URLs
        results = []
        async with ClientSession(timeout=ClientTimeout(total=timeout)) as session:
            saved = 0
            for idx, u in enumerate(candidate_urls):
                if saved >= max_results:
                    break
                try:
                    ext = os.path.splitext(urlparse(u).path)[1] or '.jpg'
                    path = os.path.join(download_folder, f"lens_{idx+1}{ext}")
                    ok = await download_image(session, u, path)
                    if ok:
                        results.append({"url": u, "file": path})
                        saved += 1
                except Exception as e:
                    print("Error saving", e)
                    continue

        # Save metadata
        with open(json_out, 'w', encoding='utf-8') as f:
            json.dump(results, f, indent=2)

        print(f"Saved {len(results)} images to {download_folder}")
        await browser.close()
        return results


if __name__ == '__main__':
    # Example usage
    # Either provide a local file path or a remote image URL
    TEST_IMAGE_PATH = "sample_for_lens/herd.png"
    TEST_IMAGE_URL = None # "https://upload.wikimedia.org/wikipedia/commons/4/4f/Cat_November_2010-1a.jpg"

    res = asyncio.run(search_with_lens(image_path=TEST_IMAGE_PATH, image_url=TEST_IMAGE_URL, max_results=10, download_folder="lens_out", timeout=20))
    print(res)
