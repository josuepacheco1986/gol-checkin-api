#!/usr/bin/env python3
import asyncio, base64, io, json, os, re, threading, time, uuid
from pathlib import Path
from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
from PIL import Image, ImageOps
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

app = Flask(__name__)
CORS(app)
JOBS = {}
OUTPUT_DIR = Path("outputs")
OUTPUT_DIR.mkdir(exist_ok=True)

JS_LOAD_H2C = """async () => {
    if (typeof html2canvas !== 'undefined') return 'ok';
    await new Promise((res, rej) => {
        const s = document.createElement('script');
        s.src = 'https://html2canvas.hertzen.com/dist/html2canvas.min.js';
        s.onload = res; s.onerror = rej;
        document.head.appendChild(s);
    });
    return 'loaded';
}"""

JS_CAPTURE = """async (idx, scale) => {
    const cards = document.querySelectorAll('.details-boarding-card');
    if (!cards[idx]) return null;
    cards[idx].scrollIntoView({ behavior: 'instant', block: 'start' });
    await new Promise(r => setTimeout(r, 600));
    const c = await html2canvas(cards[idx], {
        scale, useCORS: true, allowTaint: true,
        backgroundColor: '#ffffff', logging: false
    });
    return c.toDataURL('image/png');
}"""

JS_WAIT_PAX = """async (idx, ms) => {
    const t = Date.now();
    while (Date.now() - t < ms) {
        const it = document.querySelectorAll('.p-boarding-pass__details-item');
        if (it[idx] && it[idx].innerHTML.length > 200) return { ok: true };
        await new Promise(r => setTimeout(r, 300));
    }
    return { ok: false };
}"""

JS_CARD_COUNT = "() => document.querySelectorAll('.details-boarding-card').length"

JS_CONTINUAR = """() => {
    const btns = Array.from(document.querySelectorAll('button'));
    const b = btns.find(b => b.textContent.trim().toLowerCase() === 'continuar' && !b.disabled);
    if (b) { b.click(); return true; }
    const o = document.querySelector('.a-btn--orange:not([disabled])');
    if (o) { o.click(); return true; }
    return false;
}"""

JS_ROUTE = """(idx) => {
    const c = document.querySelectorAll('.details-boarding-card')[idx];
    if (!c) return 'TRECHO' + (idx+1);
    const stub = c.querySelector('b2c-boarding-card-stub, article');
    if (stub) {
        const m = stub.textContent.match(/([A-Z]{3})[\s\S]{0,5}([A-Z]{3})/);
        if (m && m[1] !== m[2]) return m[1] + '-' + m[2];
    }
    return 'TRECHO' + (idx+1);
}"""


class GolCheckinEngine:
    def __init__(self, payload, job_id):
        self.record_locator = payload['record_locator']
        self.departure_airport = payload['departure_airport']
        self.passengers = payload.get('passengers', [])
        self.job_id = job_id
        self.job_dir = OUTPUT_DIR / job_id
        self.job_dir.mkdir(parents=True, exist_ok=True)
        self.border_px = payload.get('border_px', 25)

    def _log(self, msg):
        ts = time.strftime('%H:%M:%S')
        line = f'[{ts}] {msg}'
        print(line, flush=True)
        JOBS[self.job_id]['logs'].append(line)

    async def _wait(self, ms=1000):
        await asyncio.sleep(ms / 1000)

    def _add_border(self, b64):
        if ',' in b64:
            b64 = b64.split(',', 1)[1]
        img = Image.open(io.BytesIO(base64.b64decode(b64))).convert('RGBA')
        bordered = ImageOps.expand(img, border=self.border_px, fill=(255,255,255,255))
        buf = io.BytesIO()
        bordered.save(buf, format='PNG', optimize=True)
        return buf.getvalue()

    async def _click_continuar(self, page, attempts=4):
        for _ in range(attempts):
            try:
                await page.wait_for_selector(
                    "button:has-text('Continuar'):not([disabled])",
                    state='visible', timeout=10_000)
                await page.click("button:has-text('Continuar'):not([disabled])")
                self._log('   -> Continuar')
                await self._wait(1200)
                return
            except Exception:
                ok = await page.evaluate(JS_CONTINUAR)
                if ok:
                    self._log('   -> Continuar JS')
                    await self._wait(1200)
                    return
                await self._wait(2000)

    async def _fill_pax(self, page, pax):
        name = pax.get('first_name','') + ' ' + pax.get('last_name','')
        self._log(f'Formulario: {name.strip()}')
        found = False
        for sel in ["button:has-text('Preencher dados')"]:
            try:
                if await page.is_visible(sel, timeout=8000):
                    await page.click(sel)
                    await self._wait(2000)
                    found = True
                    break
            except Exception:
                pass
        if not found:
            self._log('   Sem dados pendentes')
            return

        # Passo 1
        self._log('   Passo 1')
        for sel in ["select[formcontrolname*='nationality']"]:
            try:
                if await page.is_visible(sel, timeout=2000):
                    await page.select_option(sel, label=pax.get('nationality','Brasil'))
                    break
            except Exception:
                pass

        cpf = re.sub(r'\D','',pax.get('cpf',''))
        if cpf:
            cpf_fmt = re.sub(r'(\d{3})(\d{3})(\d{3})(\d{2})',r'\1.\2.\3-\4',cpf)
            for sel in ["input[formcontrolname*='cpf']","input[placeholder*='CPF']"]:
                try:
                    if await page.is_visible(sel, timeout=2000):
                        await page.triple_click(sel)
                        await page.fill(sel, cpf_fmt)
                        self._log(f'   CPF: {cpf_fmt}')
                        break
                except Exception:
                    pass

        birth = pax.get('birth_date','').strip()
        if birth:
            for sel in ["input[formcontrolname*='birth']","input[placeholder*='nascimento']"]:
                try:
                    if await page.is_visible(sel, timeout=2000):
                        await page.fill(sel, birth)
                        break
                except Exception:
                    pass

        gender = pax.get('gender','').strip()
        if gender:
            for sel in ["select[formcontrolname*='gender']","select[formcontrolname*='sex']"]:
                try:
                    if await page.is_visible(sel, timeout=2000):
                        await page.select_option(sel, label=gender)
                        break
                except Exception:
                    pass

        await self._click_continuar(page)
        await self._wait(1200)

        # Passo 2 - Contatos
        on_contacts = False
        for sel in ["input[type='email']","input[type='tel']"]:
            try:
                if await page.is_visible(sel, timeout=5000):
                    on_contacts = True
                    break
            except Exception:
                pass

        if on_contacts:
            self._log('   Passo 2')
            email = pax.get('email','').strip()
            if email:
                for sel in ["input[type='email']","input[formcontrolname*='email']"]:
                    try:
                        if await page.is_visible(sel, timeout=2000):
                            await page.fill(sel, email)
                            break
                    except Exception:
                        pass
            for sel in ["select[formcontrolname*='country']","select[formcontrolname*='pais']"]:
                try:
                    if await page.is_visible(sel, timeout=2000):
                        try: await page.select_option(sel, label='Brasil')
                        except Exception: pass
                        break
                except Exception:
                    pass
            phone = pax.get('phone','').strip()
            if phone:
                for sel in ["input[type='tel']","input[formcontrolname*='phone']","input[formcontrolname*='celular']"]:
                    try:
                        if await page.is_visible(sel, timeout=2000):
                            await page.fill(sel, phone)
                            break
                    except Exception:
                        pass
            if pax.get('skip_emergency', True):
                for sel in ["label:has-text('Prefiro nao informar')","label:has-text('nao informar contato')"]:
                    try:
                        if await page.is_visible(sel, timeout=3000):
                            await page.click(sel)
                            await self._wait(400)
                            break
                    except Exception:
                        pass
            await self._click_continuar(page)
            await self._wait(1200)

        # Passo 3 - Milhas
        for sel in ["input[formcontrolname*='fqtv']","h2:has-text('fidelidade')"]:
            try:
                if await page.is_visible(sel, timeout=4000):
                    self._log('   Passo 3 milhas - pulando')
                    await self._click_continuar(page)
                    break
            except Exception:
                pass

        self._log(f'   OK: {name.strip()}')

    async def run_checkin(self):
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=['--no-sandbox','--disable-dev-shm-usage',
                      '--disable-blink-features=AutomationControlled'])
            ctx = await browser.new_context(
                viewport={'width':1280,'height':900},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
                locale='pt-BR', timezone_id='America/Sao_Paulo')
            page = await ctx.new_page()
            await page.add_init_script("""
                Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
                window.chrome={runtime:{}};
            """)
            try:
                url = (f'https://b2c.voegol.com.br/check-in'
                       f'?recordLocator={self.record_locator}'
                       f'&departureAirport={self.departure_airport}')
                self._log(f'Navegando: {url}')
                await page.goto(url, wait_until='networkidle', timeout=90_000)
                await self._wait(2500)

                for sel in ['#onetrust-accept-btn-handler',"button:has-text('Aceitar')"]:
                    try:
                        if await page.is_visible(sel, timeout=3000):
                            await page.click(sel)
                            await self._wait(500)
                            break
                    except Exception:
                        pass

                for sel in ["button:has-text('Completar dados')"]:
                    try:
                        if await page.is_visible(sel, timeout=8000):
                            await page.click(sel)
                            self._log("Completar dados clicado")
                            await self._wait(2000)
                            break
                    except Exception:
                        pass

                for pax in self.passengers:
                    await self._fill_pax(page, pax)

                await self._wait(2000)
                if '/cartao-de-embarque' not in page.url:
                    try:
                        if await page.is_visible("button:has-text('Continuar')", timeout=6000):
                            await self._click_continuar(page)
                    except Exception:
                        pass

                if '/cartao-de-embarque' not in page.url:
                    try:
                        if await page.is_visible("input[type='checkbox']", timeout=8000):
                            self._log('Restricoes de bagagem...')
                            el = page.locator("input[type='checkbox']").first
                            if not await el.is_checked():
                                await el.click()
                            await self._wait(500)
                            await self._click_continuar(page)
                    except Exception:
                        pass

                if '/cartao-de-embarque' not in page.url:
                    try:
                        await page.wait_for_url('**/cartao-de-embarque', timeout=5000)
                    except PWTimeout:
                        self._log('Ancilares...')
                        await self._wait(4000)
                        ok = await page.evaluate(JS_CONTINUAR)
                        if not ok:
                            await self._click_continuar(page)
                        await self._wait(2000)

                if '/cartao-de-embarque' not in page.url:
                    try:
                        await page.wait_for_url('**/cartao-de-embarque', timeout=5000)
                    except PWTimeout:
                        self._log('Assentos...')
                        await self._wait(4000)
                        ok = await page.evaluate(JS_CONTINUAR)
                        if not ok:
                            await self._click_continuar(page)
                        await self._wait(6000)

                self._log('Aguardando cartao de embarque...')
                try:
                    await page.wait_for_url('**/cartao-de-embarque', timeout=90_000)
                except PWTimeout:
                    self._log('Aviso: timeout aguardando cartao')

                await self._wait(3000)
                self._log(f'CHECK-IN OK: {page.url}')

                await page.evaluate('window.scrollTo(0,0)')
                await self._wait(1000)
                saved_files = []

                for i, pax in enumerate(self.passengers):
                    pax_name = (pax.get('first_name','')+'_'+pax.get('last_name','')).upper()
                    self._log(f'Capturando cartoes: {pax_name}')

                    if i > 0:
                        tab_info = await page.evaluate(f"""
                            () => {{
                                const s = document.querySelectorAll('.m-carousel-content__item');
                                const slide = s[{i}];
                                if (!slide) return null;
                                const r = slide.getBoundingClientRect();
                                return {{ x: r.left+r.width/2, y: r.top+r.height/2 }};
                            }}
                        """)
                        if tab_info:
                            await page.evaluate('window.scrollTo(0,0)')
                            await self._wait(300)
                            await page.mouse.click(tab_info['x'], tab_info['y'])
                            await self._wait(800)
                        result = await page.evaluate(JS_WAIT_PAX, i, 15_000)
                        if not result.get('ok'):
                            self._log(f'   Aviso: conteudo pax {i+1} nao renderizou')

                    await page.evaluate(JS_LOAD_H2C)
                    await self._wait(300)
                    n_cards = await page.evaluate(JS_CARD_COUNT)

                    for ci in range(n_cards):
                        data_url = await page.evaluate(JS_CAPTURE, ci, 2)
                        if not data_url:
                            continue
                        route = await page.evaluate(JS_ROUTE, ci)
                        route = re.sub(r'[^A-Z0-9-]','',route.upper()) or f'TRECHO{ci+1}'
                        fname = f'cartao_{route}_{pax_name}.png'
                        png = self._add_border(data_url)
                        (self.job_dir / fname).write_bytes(png)
                        saved_files.append(fname)
                        self._log(f'   Salvo: {fname}')
                    await self._wait(600)

                JOBS[self.job_id]['files'] = saved_files
                JOBS[self.job_id]['status'] = 'done'
                self._log(f'Concluido! {len(saved_files)} arquivo(s)')

            except Exception as e:
                self._log(f'ERRO: {e}')
                JOBS[self.job_id]['status'] = 'error'
                JOBS[self.job_id]['error'] = str(e)
                try:
                    await page.screenshot(path=str(self.job_dir/'debug.png'))
                except Exception:
                    pass
            finally:
                await browser.close()


@app.route('/checkin', methods=['POST'])
def start_checkin():
    payload = request.get_json()
    if not payload:
        return jsonify({'error':'Payload invalido'}), 400
    job_id = str(uuid.uuid4())[:8]
    JOBS[job_id] = {'status':'running','logs':[],'files':[],'error':None}
    def run():
        asyncio.run(GolCheckinEngine(payload, job_id).run_checkin())
    threading.Thread(target=run, daemon=True).start()
    return jsonify({'job_id': job_id})

@app.route('/status/<job_id>')
def get_status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({'error':'Job nao encontrado'}), 404
    return jsonify(job)

@app.route('/download/<job_id>/<filename>')
def download_file(job_id, filename):
    safe = Path(filename).name
    path = OUTPUT_DIR / job_id / safe
    if not path.exists():
        return jsonify({'error':'Arquivo nao encontrado'}), 404
    return send_file(str(path), as_attachment=True, download_name=safe)

@app.route('/health')
def health():
    return jsonify({'ok':True,'jobs':len(JOBS)})

if __name__ == '__main__':
    print('Servidor: http://0.0.0.0:5000')
    app.run(host='0.0.0.0', port=5000, debug=False)
