"""Export publicity cards and verify local recording-page controls and layout."""
import argparse
import base64
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]


def main():
    from playwright.sync_api import sync_playwright
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url',default='http://127.0.0.1:8765')
    args=parser.parse_args()
    folder=ROOT/'docs/promo'
    output=folder/'assets'
    evidence=ROOT/'backups/development/first-test-20261015/promo'
    evidence.mkdir(parents=True,exist_ok=True)
    html=(folder/'index.html').read_text(encoding='utf-8')
    for name in ('city-radio.png','app-compose.png'):
        html=html.replace('assets/'+name,'data:image/png;base64,'+
            base64.b64encode((output/name).read_bytes()).decode())
    (folder/'B站录屏宣传页.html').write_text(html,encoding='utf-8')
    errors=[]
    browser_path=next((p for p in (Path('C:/Program Files/Google/Chrome/Application/chrome.exe'),
        Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')) if p.is_file()),None)
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,**({'executable_path':str(browser_path)} if browser_path else {}))
        page=browser.new_page(viewport=dict(width=1920,height=1080),device_scale_factor=1,
                              reduced_motion='reduce')
        page.on('pageerror',lambda error:errors.append(str(error)))
        page.goto(args.base_url,wait_until='networkidle')
        assert page.locator('section.scene').count()==6
        assert page.locator('img').evaluate_all('(imgs)=>imgs.every(i=>i.complete&&i.naturalWidth)')
        for button in page.locator('[data-lang]').all():button.click()
        assert page.locator('#foreign').inner_text()=='Ich brauche Hilfe auf der linken Seite.'
        page.keyboard.press('Home');page.keyboard.press('ArrowRight')
        assert page.locator('#counter').inner_text()=='02 / 06'
        page.keyboard.press('h');assert page.locator('body').evaluate('(b)=>b.classList.contains("recording")')
        page.keyboard.press('h');page.locator('#help').click();assert page.locator('#keys').evaluate('(d)=>d.open')
        page.locator('#dismiss').click()
        page.locator('#full').click();assert page.evaluate('Boolean(document.fullscreenElement)')
        page.evaluate('document.exitFullscreen()')
        for index,scene in enumerate(page.locator('.scene').all()):
            scene.scroll_into_view_if_needed()
            page.screenshot(path=str(evidence/f'scene-{index+1}.png'))
            assert not page.evaluate('document.documentElement.scrollWidth>innerWidth')
        page.set_viewport_size(dict(width=390,height=844))
        for index,scene in enumerate(page.locator('.scene').all()):
            scene.scroll_into_view_if_needed()
            assert not page.evaluate('document.documentElement.scrollWidth>innerWidth'),index
        page.screenshot(path=str(evidence/'mobile.png'),full_page=True)
        page.goto((folder/'B站录屏宣传页.html').as_uri(),wait_until='load')
        assert page.locator('img').evaluate_all('(imgs)=>imgs.every(i=>i.complete&&i.naturalWidth)')
        exports=[('cover',1920,1080,'bilibili-cover.png'),('cover',1600,1600,'heybox-01-cover.png'),
                 ('receive',1600,1000,'heybox-02-receive.png'),('reply',1600,1000,'heybox-03-reply.png'),
                 ('install',1600,1200,'heybox-04-install.png')]
        for card,width,height,name in exports:
            page.set_viewport_size(dict(width=width,height=height))
            page.goto(args.base_url+'/cards.html?card='+card,wait_until='networkidle')
            assert not page.evaluate('document.documentElement.scrollWidth>innerWidth')
            page.screenshot(path=str(output/name))
        assert not errors,errors
        browser.close()
    report=dict(desktop='1920x1080 pass',mobile='390x844 pass',keyboard='pass',
        fullscreen='pass',language_examples=5,standalone_offline_html='pass',
        exports=[row[-1] for row in exports],page_errors=errors)
    (evidence/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':main()
