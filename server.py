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

# Defaults para passageiros sem dados especificos
DEFAULT_CPF       = "38099424050"
DEFAULT_PHONE     = "68992297125"
DEFAULT_COUNTRY   = "55"
DEFAULT_EMAIL     = "checkin@automatico.com"
DEFAULT_NATION    = "Brasil"
DEFAULT_GENDER    = "M"
DEFAULT_BIRTHDATE = "01/01/1980"
DEFAULT_PREFER_NO_EMERGENCY = True

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
const m = stub.textContent.match(/([A-Z]{3})[\\s\\S]{0,5}([A-Z]{3})/);
if (m && m[1] !== m[2]) return m[1] + '-' + m[2];
}
return 'TRECHO' + (idx+1);
}"""


def _apply_defaults(pax):
    pax.setdefault('cpf', DEFAULT_CPF)
    pax.setdefault('phone', DEFAULT_PHONE)
    pax.setdefault('country_code', DEFAULT_COUNTRY)
    pax.setdefault('email', DEFAULT_EMAIL)
    pax.setdefault('nationality', DEFAULT_NATION)
    pax.setdefault('gender', DEFAULT_GENDER)
    pax.setdefault('birth_date', DEFAULT_BIRTHDATE)
    pax.setdefault('prefer_no_emergency', DEFAULT_PREFER_NO_EMERGENCY)
    return pax


class GolCheckinEngine:
    def __init__(self, payload, job_id):
        self.record_locator    = payload['record_locator']
        self.departure_airport = payload['departure_airport']
        raw_pax = payload.get('passengers', [{}])
        self.passengers = [_apply_defaults(dict(p)) for p in raw_pax]
        self.job_id   = job_id
        self.job_dir  = OUTPUT_DIR / job_id
        self.job_dir.mkdir(parents=True, exist_ok=True)
        self.border_px = payload.get('border_px', 25)

    def _log(self, msg):
        ts   = time.strftime('%H:%M:%S')
        line = f'[{ts}] {msg}'
        print(line, flush=True)
        JOBS[self.job_id]['logs'].append(line)

    async def _wait(self, ms=1000):
        await asyncio.sleep(ms / 1000)

    def _add_border(self, b64):
        if ',' in b64:
            b64 = b64.split(',', 1)[1]
        img      = Image.open(io.BytesIO(base64.b64decode(b64))).convert('RGBA')
        bordered = ImageOps.expand(img, border=self.border_px, fill=(255, 255, 255, 255))
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
                self._log(' -> Continuar')
                await self._wait(1200)
                return
            except Exception:
                ok = await page.evaluate(JS_CONTINUAR)
                if ok:
                    self._log(' -> Continuar JS')
                    await self._wait(1200)
                    return
            await self._wait(2000)

    async def _fill_pax(self, page, pax):
        name = pax.get('first_name', '') + ' ' + pax.get('last_name', '')
        self._log(f'Formulario: {name.strip() or "passageiro"}')
        found = False
        try:
            btns = await page.query_selector_all('button, a')
            for btn in btns:
                txt = (await btn.inner_text()).strip().lower()
                if 'preencher dados' in txt or 'completar dados' in txt:
                    await btn.click()
                    found = True
                    await self._wait(2000)
                    break
        except Exception as e:
            self._log(f'  Aviso botao: {e}')

        if not found:
            self._log('  Nenhum botao de preencher encontrado, continuando...')
            return

        # Passo 1: dados pessoais
        self._log('  Passo 1: dados pessoais')
        try:
            await page.wait_for_selector(
                'input[placeholder*="CPF"], input[name*="cpf"], input[id*="cpf"]',
                timeout=8000)
            cpf_raw = re.sub(r'\D', '', pax.get('cpf', DEFAULT_CPF))
            cpf_fmt = f"{cpf_raw[:3]}.{cpf_raw[3:6]}.{cpf_raw[6:9]}-{cpf_raw[9:]}"
            cpf_field = await page.query_selector(
                'input[placeholder*="CPF"], input[name*="cpf"], input[id*="cpf"]')
            if cpf_field:
                await cpf_field.triple_click()
                await cpf_field.type(cpf_fmt, delay=60)
        except Exception as e:
            self._log(f'  CPF erro: {e}')

        try:
            nat_sel = await page.query_selector(
                'select[name*="national"], select[id*="national"], select[formcontrolname*="national"]')
            if nat_sel:
                await nat_sel.select_option(label=pax.get('nationality', DEFAULT_NATION))
        except Exception:
            pass

        try:
            bd_field = await page.query_selector(
                'input[placeholder*="nasc"], input[name*="birth"], input[id*="birth"]')
            if bd_field:
                await bd_field.triple_click()
                await bd_field.type(pax.get('birth_date', DEFAULT_BIRTHDATE), delay=60)
        except Exception:
            pass

        try:
            gen = pax.get('gender', DEFAULT_GENDER).upper()
            gen_sel = await page.query_selector(f'input[value="{gen}"]')
            if gen_sel:
                await gen_sel.click()
        except Exception:
            pass

        await self._click_continuar(page)

        # Passo 2: contato
        self._log('  Passo 2: contato')
        try:
            email_field = await page.query_selector(
                'input[type="email"], input[name*="email"], input[id*="email"]')
            if email_field:
                await email_field.triple_click()
                await email_field.type(pax.get('email', DEFAULT_EMAIL), delay=60)
        except Exception:
            pass

        try:
            phone_raw = re.sub(r'\D', '', pax.get('phone', DEFAULT_PHONE))
            ddd = phone_raw[:2]
            num = phone_raw[2:]
            ph_field = await page.query_selector(
                'input[placeholder*="celular"], input[name*="phone"], input[id*="phone"]')
            if ph_field:
                await ph_field.triple_click()
                await ph_field.type(f"({ddd}) {num[:5]}-{num[5:]}", delay=60)
        except Exception:
            pass

        if pax.get('prefer_no_emergency', DEFAULT_PREFER_NO_EMERGENCY):
            try:
                toggle = await page.query_selector(
                    'label:has-text("emergencia"), label:has-text("emergência"), input[type="checkbox"]')
                if toggle:
                    await toggle.click()
                    await self._wait(500)
            except Exception:
                pass

        await self._click_continuar(page)

        # Passo 3: milhas (pular)
        self._log('  Passo 3: milhas (pular)')
        await self._click_continuar(page)

    async def run(self):
        self._log('Iniciando check-in GOL')
        self._log(f'Localizador: {self.record_locator} | Origem: {self.departure_airport}')
        url = (f'https://b2c.voegol.com.br/check-in'
               f'?recordLocator={self.record_locator}'
               f'&departureAirport={self.departure_airport}')

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-setuid-sandbox',
                      '--disable-blink-features=AutomationControlled',
                      '--disable-dev-shm-usage', '--disable-gpu'])
            ctx = await browser.new_context(
                user_agent=('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                            'AppleWebKit/537.36 (KHTML, like Gecko) '
                            'Chrome/124.0.0.0 Safari/537.36'),
                viewport={'width': 1280, 'height': 800},
                locale='pt-BR')
            page = await ctx.new_page()
            try:
                self._log('Acessando pagina de check-in...')
                await page.goto(url, wait_until='networkidle', timeout=60000)
                await self._wait(3000)

                # Aceitar cookies
                try:
                    await page.click(
                        "button:has-text('Aceitar'), button:has-text('Concordo')",
                        timeout=5000)
                    await self._wait(1000)
                except Exception:
                    pass

                # Completar dados / iniciar
                for sel in [
                    "button:has-text('Completar dados')",
                    "button:has-text('Iniciar check-in')",
                    "button:has-text('Continuar')"]:
                    try:
                        await page.click(sel, timeout=5000)
                        await self._wait(2000)
                        break
                    except Exception:
                        pass

                # Preencher cada passageiro
                pax_list = self.passengers if self.passengers else [_apply_defaults({})]
                for pax in pax_list:
                    await self._fill_pax(page, pax)
                    await self._wait(2000)

                # Confirmar bagagens
                self._log('Confirmando bagagens...')
                try:
                    chk = await page.query_selector('input[type="checkbox"]')
                    if chk:
                        await chk.click()
                        await self._wait(500)
                    await self._click_continuar(page)
                except Exception:
                    pass

                # Ancilares
                self._log('Ancilares...')
                try:
                    await self._click_continuar(page)
                except Exception:
                    pass

                # Assentos
                self._log('Assentos...')
                try:
                    await self._click_continuar(page)
                except Exception:
                    pass

                # Aguardar cartoes de embarque
                self._log('Aguardando cartoes de embarque...')
                try:
                    await page.wait_for_url('**/cartao-de-embarque**', timeout=60000)
                except Exception:
                    pass
                await self._wait(4000)

                # Carregar html2canvas
                await page.evaluate(JS_LOAD_H2C)
                await self._wait(2000)

                # Detectar passageiros
                pax_tabs = await page.query_selector_all(
                    '.p-boarding-pass__details-item, [class*="passenger-tab"]')
                n_pax = max(len(pax_tabs), 1)
                self._log(f'Passageiros detectados: {n_pax}')

                card_count = await page.evaluate(JS_CARD_COUNT)
                self._log(f'Cartoes por passageiro: {card_count}')

                files = []
                for pi in range(n_pax):
                    if pi > 0 and pax_tabs:
                        try:
                            await pax_tabs[pi].click()
                            await self._wait(2000)
                        except Exception:
                            pass

                    pax_name = 'PAX' + str(pi + 1)
                    try:
                        pax_els = await page.query_selector_all('.p-boarding-pass__details-item')
                        if pax_els and pi < len(pax_els):
                            nm = await pax_els[pi].inner_text()
                            pax_name = re.sub(r'\s+', '_', nm.strip().split('\n')[0].upper())[:20]
                    except Exception:
                        pass

                    card_count = await page.evaluate(JS_CARD_COUNT)
                    for ci in range(card_count):
                        route = await page.evaluate(JS_ROUTE, ci)
                        b64   = await page.evaluate(JS_CAPTURE, ci, 2)
                        if not b64:
                            continue
                        png_bytes = self._add_border(b64)
                        fname = f'cartao_{route}_{pax_name}.png'
                        fpath = self.job_dir / fname
                        fpath.write_bytes(png_bytes)
                        files.append(fname)
                        self._log(f'  Salvo: {fname} ({len(png_bytes)//1024}KB)')

                JOBS[self.job_id]['status'] = 'done'
                JOBS[self.job_id]['files']  = files
                self._log(f'Concluido! {len(files)} cartoes salvos.')

            except Exception as e:
                self._log(f'ERRO: {e}')
                JOBS[self.job_id]['status'] = 'error'
                JOBS[self.job_id]['error']  = str(e)
            finally:
                await browser.close()


def _run_job(payload, job_id):
    engine = GolCheckinEngine(payload, job_id)
    asyncio.run(engine.run())


@app.route('/checkin', methods=['POST'])
def start_checkin():
    data = request.get_json(force=True)
    if not data.get('record_locator') or not data.get('departure_airport'):
        return jsonify({'error': 'record_locator e departure_airport sao obrigatorios'}), 400
    job_id = str(uuid.uuid4())
    JOBS[job_id] = {'status': 'running', 'logs': [], 'files': [], 'error': None}
    t = threading.Thread(target=_run_job, args=(data, job_id), daemon=True)
    t.start()
    return jsonify({'job_id': job_id})


@app.route('/status/<job_id>')
def job_status(job_id):
    j = JOBS.get(job_id)
    if not j:
        return jsonify({'error': 'Job nao encontrado'}), 404
    return jsonify(j)


@app.route('/download/<job_id>/<filename>')
def download_file(job_id, filename):
    fpath = OUTPUT_DIR / job_id / filename
    if not fpath.exists():
        return jsonify({'error': 'Arquivo nao encontrado'}), 404
    return send_file(str(fpath), mimetype='image/png',
                     as_attachment=True, download_name=filename)


@app.route('/health')
def health():
    return jsonify({'ok': True, 'jobs': len(JOBS)})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
