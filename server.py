#!/usr/bin/env python3
import base64, io, json, os, re, subprocess, sys, threading, time, traceback, uuid
from pathlib import Path
from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
from PIL import Image, ImageOps

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

# ─── Script Playwright que roda em processo separado ──────────────────────────
# Isto evita o conflito asyncio/gunicorn/threading
PLAYWRIGHT_SCRIPT = """
import asyncio, base64, io, json, os, re, sys, time, traceback
from pathlib import Path
from PIL import Image, ImageOps
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

DEFAULT_CPF   = "38099424050"
DEFAULT_PHONE = "68992297125"
DEFAULT_EMAIL = "checkin@automatico.com"
DEFAULT_NATION = "Brasil"
DEFAULT_GENDER = "M"
DEFAULT_BIRTHDATE = "01/01/1980"

def log(msg):
    ts = time.strftime('%H:%M:%S')
    line = f'[{ts}] {msg}'
    print(line, flush=True)

JS_LOAD_H2C = \"\"\"async () => {
if (typeof html2canvas !== 'undefined') return 'ok';
await new Promise((res, rej) => {
const s = document.createElement('script');
s.src = 'https://html2canvas.hertzen.com/dist/html2canvas.min.js';
s.onload = res; s.onerror = rej;
document.head.appendChild(s);
});
return 'loaded';
}\"\"\"

JS_CAPTURE = \"\"\"async (idx, scale) => {
const cards = document.querySelectorAll('.details-boarding-card');
if (!cards[idx]) return null;
cards[idx].scrollIntoView({ behavior: 'instant', block: 'start' });
await new Promise(r => setTimeout(r, 600));
const c = await html2canvas(cards[idx], {
scale, useCORS: true, allowTaint: true,
backgroundColor: '#ffffff', logging: false
});
return c.toDataURL('image/png');
}\"\"\"

JS_CARD_COUNT = "() => document.querySelectorAll('.details-boarding-card').length"

JS_CONTINUAR = \"\"\"() => {
const btns = Array.from(document.querySelectorAll('button'));
const b = btns.find(b => b.textContent.trim().toLowerCase() === 'continuar' && !b.disabled);
if (b) { b.click(); return true; }
const o = document.querySelector('.a-btn--orange:not([disabled])');
if (o) { o.click(); return true; }
return false;
}\"\"\"

JS_ROUTE = \"\"\"(idx) => {
const c = document.querySelectorAll('.details-boarding-card')[idx];
if (!c) return 'TRECHO' + (idx+1);
const stub = c.querySelector('b2c-boarding-card-stub, article');
if (stub) {
const m = stub.textContent.match(/([A-Z]{3})[\\\\s\\\\S]{0,5}([A-Z]{3})/);
if (m && m[1] !== m[2]) return m[1] + '-' + m[2];
}
return 'TRECHO' + (idx+1);
}\"\"\"

def add_border(b64, px=25):
    if ',' in b64:
        b64 = b64.split(',', 1)[1]
    img = Image.open(io.BytesIO(base64.b64decode(b64))).convert('RGBA')
    bordered = ImageOps.expand(img, border=px, fill=(255, 255, 255, 255))
    buf = io.BytesIO()
    bordered.save(buf, format='PNG', optimize=True)
    return buf.getvalue()

async def click_continuar(page):
    for _ in range(4):
        try:
            await page.wait_for_selector(
                "button:has-text('Continuar'):not([disabled])",
                state='visible', timeout=10000)
            await page.click("button:has-text('Continuar'):not([disabled])")
            log(' -> Continuar')
            await asyncio.sleep(1.5)
            return
        except Exception:
            ok = await page.evaluate(JS_CONTINUAR)
            if ok:
                log(' -> Continuar JS')
                await asyncio.sleep(1.5)
                return
        await asyncio.sleep(2)

async def fill_pax(page, pax):
    log(f'Passageiro: {pax.get("first_name","")} {pax.get("last_name","")}')
    found = False
    try:
        btns = await page.query_selector_all('button, a')
        for btn in btns:
            txt = (await btn.inner_text()).strip().lower()
            if 'preencher dados' in txt or 'completar dados' in txt:
                await btn.click()
                found = True
                await asyncio.sleep(2.5)
                log('  Botao preencher clicado')
                break
    except Exception as e:
        log(f'  Aviso botao: {e}')
    if not found:
        log('  Sem botao preencher, pulando')
        return

    log('  Passo 1: dados pessoais')
    try:
        await page.wait_for_selector(
            'input[placeholder*="CPF"], input[name*="cpf"], input[id*="cpf"]',
            timeout=10000)
        cpf_raw = re.sub(r'\\D', '', pax.get('cpf', DEFAULT_CPF))
        cpf_fmt = f'{cpf_raw[:3]}.{cpf_raw[3:6]}.{cpf_raw[6:9]}-{cpf_raw[9:]}'
        cpf_field = await page.query_selector(
            'input[placeholder*="CPF"], input[name*="cpf"], input[id*="cpf"]')
        if cpf_field:
            await cpf_field.triple_click()
            await cpf_field.type(cpf_fmt, delay=60)
            log(f'  CPF: {cpf_fmt}')
    except Exception as e:
        log(f'  CPF erro: {e}')
    try:
        nat = await page.query_selector(
            'select[name*="national"], select[id*="national"]')
        if nat:
            await nat.select_option(label=pax.get('nationality', DEFAULT_NATION))
    except Exception: pass
    try:
        bd = await page.query_selector(
            'input[placeholder*="nasc"], input[name*="birth"]')
        if bd:
            await bd.triple_click()
            await bd.type(pax.get('birth_date', DEFAULT_BIRTHDATE), delay=60)
    except Exception: pass
    try:
        gen = pax.get('gender', DEFAULT_GENDER).upper()
        g = await page.query_selector(f'input[value="{gen}"]')
        if g: await g.click()
    except Exception: pass
    await click_continuar(page)

    log('  Passo 2: contato')
    try:
        ef = await page.query_selector('input[type="email"], input[name*="email"]')
        if ef:
            await ef.triple_click()
            await ef.type(pax.get('email', DEFAULT_EMAIL), delay=60)
    except Exception: pass
    try:
        ph_raw = re.sub(r'\\D', '', pax.get('phone', DEFAULT_PHONE))
        ddd = ph_raw[:2]; num = ph_raw[2:]
        pf = await page.query_selector('input[placeholder*="celular"], input[name*="phone"]')
        if pf:
            await pf.triple_click()
            await pf.type(f'({ddd}) {num[:5]}-{num[5:]}', delay=60)
    except Exception: pass
    try:
        tg = await page.query_selector(
            'label:has-text("emergencia"), label:has-text("emergência"), input[type="checkbox"]')
        if tg:
            await tg.click()
    except Exception: pass
    await click_continuar(page)

    log('  Passo 3: milhas (pular)')
    await click_continuar(page)

async def main():
    payload = json.loads(sys.argv[1])
    job_dir = Path(sys.argv[2])
    job_dir.mkdir(parents=True, exist_ok=True)
    result = {'status': 'error', 'files': [], 'logs': [], 'error': None}

    def l(msg):
        ts = time.strftime('%H:%M:%S')
        line = f'[{ts}] {msg}'
        print(line, flush=True)
        result['logs'].append(line)

    l('=== INICIANDO CHECK-IN GOL ===')
    l(f'Localizador: {payload["record_locator"]} | Origem: {payload["departure_airport"]}')
    url = (f'https://b2c.voegol.com.br/check-in'
           f'?recordLocator={payload["record_locator"]}'
           f'&departureAirport={payload["departure_airport"]}')

    pax_list = payload.get('passengers', [{}])
    for p in pax_list:
        p.setdefault('cpf', DEFAULT_CPF)
        p.setdefault('phone', DEFAULT_PHONE)
        p.setdefault('email', DEFAULT_EMAIL)
        p.setdefault('nationality', DEFAULT_NATION)
        p.setdefault('gender', DEFAULT_GENDER)
        p.setdefault('birth_date', DEFAULT_BIRTHDATE)
        p.setdefault('prefer_no_emergency', True)

    l('Iniciando Playwright...')
    async with async_playwright() as pw:
        l('Abrindo Chromium...')
        browser = await pw.chromium.launch(
            headless=True,
            args=['--no-sandbox','--disable-setuid-sandbox',
                  '--disable-dev-shm-usage','--disable-gpu',
                  '--single-process','--no-zygote',
                  '--disable-blink-features=AutomationControlled'])
        l('Chromium aberto!')
        ctx = await browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
            viewport={'width':1280,'height':800}, locale='pt-BR')
        page = await ctx.new_page()
        l('Pagina criada. Navegando...')
        try:
            try:
                await page.goto(url, wait_until='commit', timeout=30000)
                l('goto OK')
            except Exception as e:
                l(f'goto aviso: {e}')
            await asyncio.sleep(6)
            l(f'URL: {page.url}')
            l(f'Titulo: {await page.title()}')

            try:
                await page.click("button:has-text('Aceitar'), button:has-text('Concordo')", timeout=5000)
                l('Cookies aceitos')
            except Exception: pass

            l('Procurando botao inicio...')
            for sel in ["button:has-text('Completar dados')",
                        "button:has-text('Iniciar check-in')",
                        "button:has-text('Continuar')"]:
                try:
                    await page.click(sel, timeout=6000)
                    await asyncio.sleep(2)
                    l(f'Clicou: {sel}')
                    break
                except Exception: pass

            for i, pax in enumerate(pax_list):
                l(f'-- Passageiro {i+1} --')
                await fill_pax(page, pax)
                await asyncio.sleep(2)

            l('Bagagens...')
            try:
                chk = await page.query_selector('input[type="checkbox"]')
                if chk: await chk.click()
                await click_continuar(page)
            except Exception: pass

            l('Ancilares...')
            try: await click_continuar(page)
            except Exception: pass

            l('Assentos...')
            try: await click_continuar(page)
            except Exception: pass

            l('Aguardando cartao de embarque...')
            try:
                await page.wait_for_url('**/cartao-de-embarque**', wait_until='commit', timeout=90000)
                l('Chegou nos cartoes!')
            except Exception as e:
                l(f'Aviso cartao url: {e}')
            await asyncio.sleep(5)
            l(f'URL final: {page.url}')

            await page.evaluate(JS_LOAD_H2C)
            await asyncio.sleep(2)

            pax_tabs = await page.query_selector_all('.p-boarding-pass__details-item')
            n_pax = max(len(pax_tabs), 1)
            l(f'Passageiros nas abas: {n_pax}')
            card_count = await page.evaluate(JS_CARD_COUNT)
            l(f'Cartoes: {card_count}')

            files = []
            for pi in range(n_pax):
                if pi > 0 and pi < len(pax_tabs):
                    try:
                        await pax_tabs[pi].click()
                        await asyncio.sleep(2)
                    except Exception: pass
                pax_name = f'PAX{pi+1}'
                try:
                    els = await page.query_selector_all('.p-boarding-pass__details-item')
                    if els and pi < len(els):
                        nm = await els[pi].inner_text()
                        pax_name = re.sub(r'\\s+','_',nm.strip().split('\\n')[0].upper())[:20]
                except Exception: pass
                card_count = await page.evaluate(JS_CARD_COUNT)
                for ci in range(card_count):
                    route = await page.evaluate(JS_ROUTE, ci)
                    b64 = await page.evaluate(JS_CAPTURE, ci, 2)
                    if not b64: continue
                    png = add_border(b64)
                    fname = f'cartao_{route}_{pax_name}.png'
                    (job_dir / fname).write_bytes(png)
                    files.append(fname)
                    l(f'Salvo: {fname} ({len(png)//1024}KB)')

            result['status'] = 'done'
            result['files'] = files
            l(f'=== CONCLUIDO! {len(files)} cartoes ===')
        except Exception as e:
            tb = traceback.format_exc()
            l(f'ERRO: {e}')
            l(f'TB: {tb[:300]}')
            result['error'] = str(e)
        finally:
            await browser.close()
    print('RESULT:' + json.dumps(result), flush=True)

asyncio.run(main())
"""

def _add_border(self, b64, px=25):
    if ',' in b64:
        b64 = b64.split(',', 1)[1]
    img = Image.open(io.BytesIO(base64.b64decode(b64))).convert('RGBA')
    bordered = ImageOps.expand(img, border=px, fill=(255, 255, 255, 255))
    buf = io.BytesIO()
    bordered.save(buf, format='PNG', optimize=True)
    return buf.getvalue()


def _run_job(payload, job_id):
    """Roda o check-in em PROCESSO SEPARADO para evitar conflito asyncio/gunicorn."""
    job_dir = str(OUTPUT_DIR / job_id)
    logs = JOBS[job_id]['logs']

    def _log(msg):
        ts = time.strftime('%H:%M:%S')
        line = f'[{ts}] {msg}'
        print(line, flush=True)
        logs.append(line)

    _log('Iniciando check-in GOL')
    _log(f'Localizador: {payload["record_locator"]} | Origem: {payload["departure_airport"]}')

    try:
        # Escreve o script em arquivo temporario
        script_path = OUTPUT_DIR / f'{job_id}_script.py'
        script_path.write_text(PLAYWRIGHT_SCRIPT)

        _log('Iniciando processo Playwright...')
        proc = subprocess.Popen(
            [sys.executable, str(script_path), json.dumps(payload), job_dir],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )

        result_line = None
        for line in proc.stdout:
            line = line.rstrip()
            if line.startswith('RESULT:'):
                result_line = line[7:]
            else:
                _log(line.replace('[', '').replace(']', '') if line.startswith('[') else line)
                logs[-1] = line  # substituir com linha original formatada

        proc.wait()
        script_path.unlink(missing_ok=True)

        if result_line:
            result = json.loads(result_line)
            JOBS[job_id]['status'] = result.get('status', 'error')
            JOBS[job_id]['files']  = result.get('files', [])
            JOBS[job_id]['error']  = result.get('error')
            # Adicionar logs do subprocess ao job
            for l in result.get('logs', []):
                if l not in logs:
                    logs.append(l)
        else:
            _log(f'Processo terminou sem resultado. Codigo: {proc.returncode}')
            JOBS[job_id]['status'] = 'error'
            JOBS[job_id]['error']  = f'Processo terminou sem resultado (code {proc.returncode})'

    except Exception as e:
        tb = traceback.format_exc()
        _log(f'ERRO no processo: {e}')
        _log(tb[:300])
        JOBS[job_id]['status'] = 'error'
        JOBS[job_id]['error']  = str(e)


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
