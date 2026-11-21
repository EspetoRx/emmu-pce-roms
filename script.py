import asyncio
from collections import deque
import json
from pathlib import Path
import re
import time
from urllib.parse import urljoin, urlparse
import zipfile
from playwright.async_api import async_playwright

LIST_URL = "https://www.planetemu.net/en/roms/nec-pc-engine"
BASE_DIR = Path(__file__).resolve().parent
ROMS_DIR = BASE_DIR / "roms"
HISTORY_FILE = BASE_DIR / "downloaded_roms.json"

# Configurações equilibradas para respeitar os limites do Planetemu sem esperas excessivas
MAX_CONCURRENT_PAGES = 1          # 1 por vez evita sobrecarga e bloqueios
MAX_DOWNLOADS_PER_MINUTE = 10     # Teto seguro de downloads iniciados por minuto
MIN_DELAY_BETWEEN_STARTS = 4.0    # Pausa mínima (em segundos) entre o início de cada download
MAX_WAIT_CAP = 12.0               # Teto máximo de espera do rate limiter por item
MAX_RETRIES = 3


class RateLimiter:
    """Garante que não sejam iniciados mais downloads por minuto do que o permitido,
    com teto de espera para não acumular atrasos longos."""

    def __init__(self, max_per_minute: int, min_interval: float, max_wait: float = 12.0):
        self.max_per_minute = max_per_minute
        self.min_interval = min_interval
        self.max_wait = max_wait
        self.timestamps = deque()
        self.last_start = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            now = time.monotonic()

            while self.timestamps and now - self.timestamps[0] >= 60.0:
                self.timestamps.popleft()

            elapsed_since_last = now - self.last_start
            wait_for_interval = max(0.0, self.min_interval - elapsed_since_last)

            wait_for_minute_cap = 0.0
            if len(self.timestamps) >= self.max_per_minute:
                wait_for_minute_cap = max(0.0, 60.0 - (now - self.timestamps[0]) + 1.0)

            wait_time = min(max(wait_for_interval, wait_for_minute_cap), self.max_wait)
            if wait_time > 0.2:
                print(f"  [Rate Limit] Aguardando {wait_time:.1f}s...")
                await asyncio.sleep(wait_time)

            current = time.monotonic()
            self.timestamps.append(current)
            self.last_start = current


def load_history() -> dict:
    if HISTORY_FILE.exists():
        try:
            with HISTORY_FILE.open("r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    data.setdefault("completed_urls", [])
                    data.setdefault("completed_files", [])
                    return data
        except Exception:
            pass
    return {"completed_urls": [], "completed_files": []}


def save_history(history: dict):
    with HISTORY_FILE.open("w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", name).strip()


def arquivo_ja_existe(path: Path) -> bool:
    return path.exists() and path.is_file() and path.stat().st_size > 0


def ja_foi_baixado(url: str, expected_name: str, history: dict) -> bool:
    completed_urls = set(history.get("completed_urls", []))
    completed_files = set(history.get("completed_files", []))

    if url in completed_urls:
        return True
    if expected_name:
        if expected_name in completed_files:
            return True
        if arquivo_ja_existe(ROMS_DIR / expected_name):
            return True
    return False


def marcar_como_baixado(url: str, filename: str, history: dict, lock: asyncio.Lock):
    async def _save():
        async with lock:
            completed_urls = set(history.get("completed_urls", []))
            completed_files = set(history.get("completed_files", []))
            completed_urls.add(url)
            if filename:
                completed_files.add(filename)
            history["completed_urls"] = sorted(completed_urls)
            history["completed_files"] = sorted(completed_files)
            save_history(history)

    return _save()


def extrair_e_remover_zip(zip_path: Path, dest_dir: Path) -> bool:
    """Extrai o .zip em dest_dir e apaga o arquivo .zip original se tudo correr bem."""
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            bad_file = zf.testzip()
            if bad_file is not None:
                print(f"  [ZIP] Arquivo corrompido dentro de '{zip_path.name}': {bad_file} — mantendo .zip.")
                return False

            zf.extractall(dest_dir)

        zip_path.unlink()
        print(f"  [ZIP] Extraído em '{dest_dir.name}/' e original removido: {zip_path.name}")
        return True

    except zipfile.BadZipFile:
        print(f"  [ZIP] '{zip_path.name}' não é um ZIP válido ou está incompleto — mantendo arquivo.")
        return False
    except Exception as exc:
        print(f"  [ZIP] Erro ao extrair '{zip_path.name}': {exc} — mantendo arquivo.")
        return False


def is_pce_game_url(full_url: str) -> bool:
    parsed = urlparse(full_url)
    if "planetemu.net" not in parsed.netloc.lower():
        return False

    path = parsed.path.rstrip("/").lower()
    return "/en/rom/nec-pc-engine/" in path


async def coletar_links_jogos(page) -> list[str]:
    print(f"Abrindo catálogo: {LIST_URL}")
    await page.goto(LIST_URL, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(2000)

    hrefs = await page.locator("a[href]").evaluate_all(
        "els => els.map(e => e.getAttribute('href')).filter(Boolean)"
    )

    urls = []
    seen = set()
    for h in hrefs:
        full_url = urljoin(LIST_URL, h).split("#")[0]
        if is_pce_game_url(full_url) and full_url not in seen:
            seen.add(full_url)
            urls.append(full_url)

    return urls


async def localizar_botao_download(game_page):
    """Localiza o input submit de download ou botão/link visível equivalente."""
    input_btn = game_page.locator(
        "input[type='submit'][name='download']:visible, "
        "input[type='submit'][value^='Download ' i]:visible"
    ).first
    if await input_btn.count() > 0:
        label = (await input_btn.get_attribute("value") or "").strip()
        return input_btn, label

    fallback_btn = (
        game_page.locator("a:visible, button:visible")
        .filter(has_text=re.compile(r"^\s*Download\s+(?!limits\b)\S+", re.I))
        .first
    )
    label = " ".join((await fallback_btn.inner_text()).split()).strip()
    return fallback_btn, label


async def disparar_e_capturar_download(context, game_page, download_btn):
    """Captura download na mesma página ou em nova aba caso o clique abra uma."""
    await download_btn.scroll_into_view_if_needed()
    await game_page.wait_for_timeout(300)

    try:
        async with game_page.expect_download(timeout=45000) as page_download_info:
            await download_btn.click()
        return await page_download_info.value
    except Exception:
        # Verifica se abriu alguma página nova no contexto
        for p in context.pages:
            if p != game_page:
                try:
                    async with p.expect_download(timeout=20000) as popup_download_info:
                        pass
                    dl = await popup_download_info.value
                    try:
                        await p.close()
                    except Exception:
                        pass
                    return dl
                except Exception:
                    continue
        raise


async def baixar_rom(
    context,
    semaphore: asyncio.Semaphore,
    limiter: RateLimiter,
    history: dict,
    history_lock: asyncio.Lock,
    idx: int,
    total: int,
    url: str,
):
    if ja_foi_baixado(url, "", history):
        print(f"[{idx}/{total}] Já registrado como baixado — pulando: {url}")
        return

    async with semaphore:
        for attempt in range(1, MAX_RETRIES + 1):
            game_page = await context.new_page()
            try:
                print(f"\n[{idx}/{total}] (Tentativa {attempt}/{MAX_RETRIES}) Abrindo: {url}")
                await game_page.goto(url, wait_until="domcontentloaded", timeout=60000)
                await game_page.wait_for_timeout(800)

                download_btn, btn_text = await localizar_botao_download(game_page)
                await download_btn.wait_for(state="visible", timeout=20000)

                expected_name = ""
                match = re.match(r"^\s*Download\s+(.+)$", btn_text, re.I)
                if match:
                    expected_name = sanitize_filename(match.group(1).strip())

                if ja_foi_baixado(url, expected_name, history):
                    print(f"  [{idx}/{total}] Já baixado anteriormente ({expected_name}) — pulando.")
                    await marcar_como_baixado(url, expected_name, history, history_lock)
                    return

                await limiter.acquire()

                download = await disparar_e_capturar_download(context, game_page, download_btn)

                suggested = sanitize_filename(
                    download.suggested_filename or expected_name or f"pce_rom_{idx}.zip"
                )
                dest_path = ROMS_DIR / suggested

                if ja_foi_baixado(url, suggested, history):
                    print(f"  [{idx}/{total}] Arquivo já presente/registrado: {dest_path.name} — pulando.")
                    try:
                        await download.cancel()
                    except Exception:
                        pass
                    await marcar_como_baixado(url, suggested, history, history_lock)
                    return

                await download.save_as(str(dest_path))
                print(f"  [{idx}/{total}] Salvo com sucesso em: {dest_path}")

                if dest_path.suffix.lower() == ".zip":
                    extrair_e_remover_zip(dest_path, ROMS_DIR)

                await marcar_como_baixado(url, suggested, history, history_lock)
                return

            except Exception as exc:
                print(f"  [{idx}/{total}] Falha na tentativa {attempt}: {exc}")
                if attempt < MAX_RETRIES:
                    backoff = 10 * attempt
                    print(f"  [{idx}/{total}] Aguardando {backoff}s antes de tentar novamente...")
                    await asyncio.sleep(backoff)
                else:
                    print(f"  [{idx}/{total}] Esgotadas as tentativas para: {url}")
            finally:
                try:
                    await game_page.close()
                except Exception:
                    pass


async def main():
    ROMS_DIR.mkdir(parents=True, exist_ok=True)
    history = load_history()
    history_lock = asyncio.Lock()

    limiter = RateLimiter(
        max_per_minute=MAX_DOWNLOADS_PER_MINUTE,
        min_interval=MIN_DELAY_BETWEEN_STARTS,
        max_wait=MAX_WAIT_CAP,
    )
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_PAGES)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(accept_downloads=True)
        list_page = await context.new_page()

        game_urls = await coletar_links_jogos(list_page)
        await list_page.close()

        total = len(game_urls)
        print(f"Total de itens encontrados: {total}")
        print(f"Já registrados em histórico: {len(history.get('completed_urls', []))}")
        print(f"Destino das ROMs: {ROMS_DIR}")

        if total == 0:
            await browser.close()
            print("Nenhum link de ROM encontrado. Verifique a estrutura da página.")
            return

        tasks = [
            baixar_rom(context, semaphore, limiter, history, history_lock, idx, total, url)
            for idx, url in enumerate(game_urls, start=1)
        ]
        await asyncio.gather(*tasks)

        await browser.close()
        print("\nDownload finalizado!")


if __name__ == "__main__":
    asyncio.run(main())
