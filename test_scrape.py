import asyncio
from playwright.async_api import async_playwright
from bot.scraper import extract_fields_from_page


async def test(url, label):
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        pg = await b.new_page()
        try:
            await pg.goto(url, wait_until="networkidle", timeout=45000)
            await pg.wait_for_timeout(2500)
            fields = await extract_fields_from_page(pg)
            labels = [getattr(f, "label", "?")[:20] for f in fields][:6]
            print(label + ": " + str(len(fields)) + " fields -> " + str(labels))
        except Exception as e:
            print(label + ": ERROR " + str(e)[:90])
        await b.close()


async def main():
    base = "https://job-boards.greenhouse.io/arizeai/jobs/6030953004"
    await test(base, "BASE")
    await test(base + "/application", "APPLICATION")


asyncio.run(main())
