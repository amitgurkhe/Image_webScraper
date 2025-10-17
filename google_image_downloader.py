import os
import time
import random
import string
import json
import pathlib
from urllib.parse import quote_plus
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.chrome.options import Options
import requests
from tqdm import tqdm

# ------------------ CONFIG ------------------
# Prefer a path relative to this script so chromedriver is found regardless
# of the current working directory. This builds an absolute path pointing
# to the bundled chromedriver executable.
CHROME_DRIVER_PATH = os.path.join(os.path.dirname(__file__), "chromedriver-win64", "chromedriver.exe")
DOWNLOAD_ROOT = "downloads"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}
# Conservative defaults — adjust down if you run into blocks
MIN_DELAY = 0.8
MAX_DELAY = 3.0

# How many images to fetch per query (you requested ~1000; be cautious)
DEFAULT_MAX_IMAGES = 100

# Preview image count before you confirm full run
PREVIEW_COUNT = 10

# Optional proxies (None or dict like {"http": "http://...", "https": "https://..."})
REQUESTS_PROXIES = None

# Optional: rotate user agents (small sample)
USER_AGENTS = [
    HEADERS["User-Agent"],
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.4 Safari/605.1.15"
]

# --------------------------------------------

def random_sleep(a=MIN_DELAY, b=MAX_DELAY):
    time.sleep(random.uniform(a, b))

def human_scroll(driver, times=3, pause=1.0):
    """Scroll by small random amounts with pauses to simulate human reading."""
    for _ in range(times):
        driver.execute_script("window.scrollBy(0, Math.floor(window.innerHeight * (0.6 + Math.random()*0.6)));")
        time.sleep(pause + random.random())

def small_mouse_move(driver, el=None):
    """Make small random mouse movements."""
    try:
        ac = ActionChains(driver)
        # if element provided, move near element, else move random coords
        if el:
            ac.move_to_element_with_offset(el, random.randint(1, 20), random.randint(1,20)).perform()
        else:
            w = driver.execute_script("return window.innerWidth")
            h = driver.execute_script("return window.innerHeight")
            x = random.randint(0, max(1, w-1))
            y = random.randint(0, max(1, h-1))
            ac.move_by_offset(x, y).perform()
            ac.move_by_offset(-x//2, -y//2).perform()
        time.sleep(0.2 + random.random()*0.5)
    except Exception:
        pass

def setup_driver(headful=True, window_size=None):
    opts = Options()
    if not headful:
        opts.add_argument("--headless=new")
    # don't use automation flags that are obviously bot-like
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)

    # randomize viewport
    if not window_size:
        widths = [1200, 1366, 1440, 1536]
        heights = [700, 768, 800, 900]
        window_size = f"{random.choice(widths)},{random.choice(heights)}"
    opts.add_argument(f"--window-size={window_size}")

    # disable images? we need images, so keep enabled
    # add other safe flags
    opts.add_argument("--disable-blink-features=AutomationControlled")
    # set language
    opts.add_argument("--lang=en-GB")

    service = ChromeService(executable_path=CHROME_DRIVER_PATH)
    driver = webdriver.Chrome(service=service, options=opts)
    # short implicit wait to help find elements that load slightly later
    try:
        driver.implicitly_wait(2)
    except Exception:
        pass

    # try to remove webdriver property
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": """
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            """
        })
    except Exception:
        pass

    return driver

def build_google_images_url(query):
    return f"https://www.google.com/search?q={quote_plus(query)}&tbm=isch"

def ensure_folder(path):
    os.makedirs(path, exist_ok=True)

def filename_for_url(url):
    safe = ''.join(c for c in url if c.isalnum() or c in (' ','.','_','-')).rstrip()
    if len(safe) > 180:
        safe = safe[:180]
    rand = ''.join(random.choice(string.ascii_letters + string.digits) for _ in range(6))
    ext = pathlib.Path(url.split('?')[0]).suffix or ".jpg"
    return f"{rand}_{safe[:100]}{ext}"

def download_image(url, dest_path, proxies=None, timeout=15):
    # choose random UA for each request
    headers = {"User-Agent": random.choice(USER_AGENTS), "Referer": "https://www.google.com/"}
    try:
        r = requests.get(url, headers=headers, stream=True, timeout=timeout, proxies=proxies)
        if r.status_code == 200:
            content_type = r.headers.get("Content-Type", "")
            if "image" not in content_type:
                return False
            with open(dest_path, "wb") as f:
                for chunk in r.iter_content(1024*8):
                    if chunk:
                        f.write(chunk)
            return True
    except Exception as e:
        # print or log error
        return False
    return False

def create_preview_html(query, image_paths, out_html):
    html = ["<html><head><meta charset='utf-8'><title>Preview</title></head><body>"]
    html.append(f"<h2>Preview for query: {query} — {len(image_paths)} images</h2>")
    html.append("<div style='display:flex;flex-wrap:wrap;'>")
    for p in image_paths:
        rel = os.path.relpath(p, os.path.dirname(out_html))
        html.append(f"<div style='margin:4px'><img src='{rel}' style='width:200px;height:200px;object-fit:cover' /><div style='font-size:11px'>{os.path.basename(p)}</div></div>")
    html.append("</div></body></html>")
    with open(out_html, "w", encoding="utf-8") as f:
        f.write("\n".join(html))
    return out_html

def fetch_image_urls_for_query(driver, query, max_images=DEFAULT_MAX_IMAGES, scroll_pause=1.0, max_tries=10):
    """Return list of candidate image URLs (direct src links) found on Google Images page."""
    url = build_google_images_url(query)
    driver.get(url)
    # try to dismiss common consent / cookie banners that block the view
    try:
        for text in ("I agree", "I accept", "Accept all", "Agree", "GOT IT", "Got it"):
            try:
                btns = driver.find_elements(By.XPATH, f"//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{text.lower()}')]")
                if btns:
                    btns[0].click()
                    random_sleep(0.5, 1.2)
                    break
            except Exception:
                continue
    except Exception:
        pass
    random_sleep(1.0, 2.5)
    small_mouse_move(driver)

    image_urls = set()
    last_height = driver.execute_script("return document.body.scrollHeight")
    tries = 0

    while len(image_urls) < max_images and tries < max_tries:
        # scroll and wait
        human_scroll(driver, times=2, pause=scroll_pause)
        small_mouse_move(driver)
        # find thumbnails (try a couple of selectors — Google changes DOM occasionally)
        thumbs = driver.find_elements(By.CSS_SELECTOR, "img.Q4LuWd")  # google thumbnail class (subject to change)
        if not thumbs:
            thumbs = driver.find_elements(By.CSS_SELECTOR, "a.wXeWr.islib.nfEiy")
        if not thumbs:
            thumbs = driver.find_elements(By.CSS_SELECTOR, "img")[:80]
        # debug: report how many thumbnails we found this pass
        try:
            print(f"Found {len(thumbs)} thumbnails on this pass")
        except Exception:
            pass
        for t in thumbs:
            try:
                # move to element to trigger lazy load
                ActionChains(driver).move_to_element(t).perform()
                random_sleep(0.15, 0.6)
                src = t.get_attribute("src")
                data_src = t.get_attribute("data-src")
                data_iurl = t.get_attribute("data-iurl")
                # prefer direct data-src or data-iurl if available (some thumbnails embed the full URL)
                if data_src and data_src.startswith("http"):
                    image_urls.add(data_src)
                elif data_iurl and data_iurl.startswith("http"):
                    image_urls.add(data_iurl)
                elif src and src.startswith("http"):
                    image_urls.add(src)
            except Exception:
                continue

        # click thumbnail to get full-size url from the right pane
        for t in thumbs[:min(len(thumbs), 40)]:
            try:
                t.click()
                # give the preview area more time to load a full-size image
                random_sleep(1.2, 2.0)
                # the full-size image sometimes appears in img.n3VNCb
                full_imgs = driver.find_elements(By.CSS_SELECTOR, "img.n3VNCb")
                for fi in full_imgs:
                    try:
                        # prefer srcset entries with the largest width when available
                        srcset = fi.get_attribute("srcset") or fi.get_attribute("data-srcset") or ""
                        candidate = None
                        if srcset:
                            parts = [p.strip() for p in srcset.split(',') if p.strip()]
                            best = None
                            best_w = 0
                            for p in parts:
                                seg = p.rsplit(' ', 1)
                                if len(seg) == 2 and seg[1].endswith('w'):
                                    try:
                                        w = int(seg[1][:-1])
                                        urlp = seg[0]
                                        if w > best_w:
                                            best_w = w
                                            best = urlp
                                    except Exception:
                                        continue
                            if best:
                                candidate = best
                        if not candidate:
                            candidate = fi.get_attribute("src")
                        if candidate and candidate.startswith("http") and "encrypted" not in candidate and not candidate.startswith("data:"):
                            # check rendered naturalWidth to prefer larger images
                            try:
                                nw = driver.execute_script("return arguments[0].naturalWidth || 0;", fi)
                            except Exception:
                                nw = 0
                            if nw >= 300:
                                image_urls.add(candidate)
                            else:
                                # add as fallback but prefer larger images; this allows some results even if small
                                image_urls.add(candidate)
                    except Exception:
                        continue
                random_sleep(0.2, 0.6)
            except Exception:
                continue

        # attempt to click "Show more results" if present
        try:
            more = driver.find_element(By.CSS_SELECTOR, ".mye4qd")
            if more.is_displayed():
                more.click()
                random_sleep(1.0, 2.0)
        except Exception:
            pass

        # check if page height changed
        new_height = driver.execute_script("return document.body.scrollHeight")
        if new_height == last_height:
            tries += 1
        else:
            last_height = new_height
            tries = 0

    return list(image_urls)[:max_images]

def download_for_query(query, max_images=DEFAULT_MAX_IMAGES, preview_only=False):
    query_safe = query.replace("/", "_").replace("\\", "_")
    folder = os.path.join(DOWNLOAD_ROOT, query_safe)
    ensure_folder(folder)

    driver = setup_driver(headful=True)
    try:
        urls = fetch_image_urls_for_query(driver, query, max_images=max_images)
    finally:
        # keep the driver open if we are going to preview in browser; close later as needed
        driver.quit()

    print(f"Found {len(urls)} candidate image URLs for '{query}'")

    # Preview step: download first PREVIEW_COUNT small images for user validation
    preview_count = min(PREVIEW_COUNT, len(urls))
    preview_paths = []
    for i, url in enumerate(urls[:preview_count]):
        fname = filename_for_url(url)
        dest = os.path.join(folder, f"preview_{i+1}_{fname}")
        if not os.path.exists(dest):
            ok = download_image(url, dest, proxies=REQUESTS_PROXIES)
            random_sleep(0.2, 0.8)
        else:
            ok = True
        if ok:
            preview_paths.append(dest)

    preview_html = os.path.join(folder, f"preview_{query_safe}.html")
    create_preview_html(query, preview_paths, preview_html)
    print(f"Preview created: {preview_html}")
    print("Open the preview HTML to confirm results. If correct, run with preview_only=False to download full set.")

    if preview_only:
        return

    # Download full set (resumable)
    for i, url in enumerate(tqdm(urls, desc=f"Downloading {query}", unit="img")):
        try:
            fname = filename_for_url(url)
            dest = os.path.join(folder, fname)
            if os.path.exists(dest):
                continue
            # polite sleep
            random_sleep(MIN_DELAY, MAX_DELAY)
            success = download_image(url, dest, proxies=REQUESTS_PROXIES)
            if not success:
                # try a couple more times
                for _ in range(2):
                    random_sleep(0.8, 1.8)
                    if download_image(url, dest, proxies=REQUESTS_PROXIES):
                        success = True
                        break
            # small random mouse move simulation (no effect on requests)
            # (This is just to simulate human actions if you are running the driver in parallel)
            # small_mouse_move(None)
        except Exception:
            continue

def run_queries(queries, max_images_each=DEFAULT_MAX_IMAGES, preview_only=True):
    # Run preview for each query
    for q in queries:
        print("\n" + "="*40)
        print(f"Query: {q}")
        download_for_query(q, max_images=max_images_each, preview_only=preview_only)
        # be polite between queries
        random_sleep(2.0, 5.0)

if __name__ == "__main__":
    # Example usage
    queries = [
        "damaged personal laptops, phones and watches"
        # "modern living room interior",
        # add your queries here
    ]
    # First, preview mode: it will create preview HTMLs for each query
    run_queries(queries, max_images_each=200, preview_only=False)

    # AFTER you inspect previews and confirm, run full download:
    # run_queries(queries, max_images_each=1000, preview_only=False)
