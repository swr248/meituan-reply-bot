
import sys, json, time, os
from playwright.sync_api import sync_playwright

state_dir = "/home/ubuntu/.meituan-reply-bot-shop2/state"
cookie_file = os.path.join(state_dir, "cookies.json")
with open(cookie_file) as f:
    cookies = json.load(f)

CHAT_URL = "https://shangoue.meituan.com/imworkbench/home?appId=4&clientType=150006#/im/page/workbench/reception"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
    ctx = browser.new_context()
    ctx.add_cookies(cookies)
    page = ctx.new_page()
    page.goto(CHAT_URL, wait_until="networkidle", timeout=30000)
    time.sleep(3)

    result = page.evaluate(r"""() => {
        const all = document.querySelectorAll("*");
        const cards = [];
        for (const el of all) {
            const t = (el.innerText || "").trim();
            if ((t.includes("超时未回复") || t.includes("待回复") || t.includes("接待中")) && t.length < 200) {
                cards.push({
                    tag: el.tagName,
                    cls: (el.className || "").toString().substring(0, 120),
                    text: t.substring(0, 200),
                    html: el.outerHTML.substring(0, 1500),
                    children: el.children.length,
                });
            }
        }
        const countdownEls = [];
        for (const el of all) {
            const t = (el.innerText || "").trim();
            const cls = (el.className || "").toString().toLowerCase();
            if (/\d+s/.test(t) || cls.includes("countdown") || cls.includes("timer") ||
                cls.includes("count-down") || cls.includes("timeout") || cls.includes("badge")) {
                const style = window.getComputedStyle(el);
                countdownEls.push({
                    tag: el.tagName,
                    cls: (el.className || "").toString().substring(0, 120),
                    text: t.substring(0, 80),
                    html: el.outerHTML.substring(0, 500),
                });
            }
        }
        return { cards: cards.slice(0, 3), countdown: countdownEls.slice(0, 10) };
    }""")

    print(json.dumps(result, ensure_ascii=False, indent=2))
    browser.close()
