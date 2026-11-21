"""
앱 아이콘 만들기 — 웹 화면의 favicon(vite-project/public/favicon.svg)으로 트레이·exe 아이콘과 시작 화면을 만든다.

  assets/klaw.png         트레이 아이콘 (256x256, 투명 배경)
  assets/klaw.ico         exe 아이콘 (16~256 여러 크기)
  assets/splash.png       exe 시작 화면 (압축을 푸는 동안 보인다)

SVG를 그리려면 브라우저가 필요해 Playwright + Windows 기본 Edge를 쓴다 (빌드할 때만 필요).
favicon.svg를 바꾸면 이 스크립트를 다시 실행하면 된다 (scripts/build_exe.py가 아이콘이 없을 때 자동으로 부른다).

    uv run python scripts/make_icons.py
"""
import asyncio
import base64
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
SVG = ROOT / "vite-project" / "public" / "favicon.svg"
OUT = ROOT / "assets"


async def render_svg(size: int) -> Image.Image:
    from playwright.async_api import async_playwright
    # base64로 넣는다 — 그대로 넣으면 SVG 안의 따옴표·#·한글 때문에 이미지가 깨질 수 있다
    svg = base64.b64encode(SVG.read_bytes()).decode()
    html = (f"<html><body style='margin:0;background:transparent'>"
            f"<div style='width:{size}px;height:{size}px;display:flex;align-items:center;justify-content:center'>"
            f"<img src='data:image/svg+xml;base64,{svg}' "
            f"style='width:{int(size * 0.92)}px;height:{int(size * 0.92)}px;object-fit:contain'></div></body></html>")
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="msedge", headless=True)
        page = await browser.new_page(viewport={"width": size, "height": size})
        await page.set_content(html)
        await page.wait_for_timeout(200)
        path = OUT / "_render.png"
        await page.screenshot(path=str(path), omit_background=True)
        await browser.close()
    img = Image.open(path).convert("RGBA")
    path.unlink()
    return img


def splash(icon: Image.Image) -> Image.Image:
    """시작 화면: 아이콘 + 이름 + 안내 (흰 바탕 둥근 카드)."""
    w, h = 420, 240
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=18, fill=(255, 255, 255, 255), outline=(227, 230, 234, 255))
    img.alpha_composite(icon.resize((88, 88), Image.LANCZOS), ((w - 88) // 2, 34))
    font = None
    for name in ("malgunbd.ttf", "malgun.ttf", "arial.ttf"):
        try:
            font = ImageFont.truetype(name, 26)
            small = ImageFont.truetype(name.replace("bd", ""), 14)
            break
        except OSError:
            continue
    font = font or ImageFont.load_default()
    small = small if font else ImageFont.load_default()
    for text, f, y, color in (("KLaw", font, 136, (26, 34, 51)), ("국내법 상담 챗봇을 시작하는 중…", small, 178, (107, 114, 128))):
        tw = d.textlength(text, font=f)
        d.text(((w - tw) / 2, y), text, font=f, fill=color)
    return img


def main() -> int:
    if not SVG.exists():
        print(f"favicon이 없습니다: {SVG}")
        return 1
    OUT.mkdir(exist_ok=True)
    icon = asyncio.run(render_svg(256))
    icon.save(OUT / "klaw.png")
    icon.save(OUT / "klaw.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    splash(icon).save(OUT / "splash.png")
    print("만듦:", ", ".join(str(p.relative_to(ROOT)) for p in (OUT / "klaw.png", OUT / "klaw.ico", OUT / "splash.png")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
