import asyncio
import json
from pathlib import Path
import re
from urllib.parse import urljoin, urlparse
import zipfile
from playwright.async_api import async_playwright

LIST_URL = "https://www.planetemu.net/en/roms/nec-pc-engine"
BASE_DIR = Path(__file__).resolve().parent
ROMS_DIR = BASE_DIR / "roms"
HISTORY_FILE = BASE_DIR / "downloaded_roms.json"
PROFILE_DIR = BASE_DIR / ".browser_profile"

MAX_CONCURRENT_PAGES = 1
MIN_DELAY_BETWEEN_DOWNLOADS = 3   # Pausa mínima padrão (segundos) entre downloads
MAX_CAPTCHA_WAIT_SECONDS = 600    # Tempo máximo aguardando você resolver o desafio (10 min)
MAX_RETRIES = 3


def normalize_key(name: str) -> str:
    """Normaliza o nome base (sem extensão) para comparar .zip com arquivos extraídos."""
    stem = Path(name).stem.lower()
    return re.sub(r"[^a-z0-9]+", "", stem)


def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", name).strip()


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


def coletar_arquivos_existentes_na_pasta() -> tuple[set[str], set[str]]:
    """Lê todos os arquivos já presentes em roms/ (nomes exatos e nomes-base normalizados)."""
    exact_names = set()
    normalized_stems = set()
    if ROMS_DIR.exists():
        for p in ROMS_DIR.iterdir():
            if p.is_file() and p.stat().st_size > 0:
                exact_names.add(p.name)
                norm = normalize_key(p.name)
                if norm:
                    normalized_stems.add(norm)
    return exact_names, normalized_stems


def ja_foi_baixado(
    url: str,
    expected_name: str,
    history: dict,
    existing_exact: set[str],
    existing_stems: set[str],
) -> bool:
    if url in set(history.get("completed_urls", [])):
        return True

    if expected_name:
        if expected_name in set(history.get("completed_files", [])):
            return True
        if expected_name in existing_exact:
            return True
        norm = normalize_key(expected_name)
        if norm and norm in existing_stems:
            return True

    return False


async def marcar_como_baixado(
    url: str,
    filename: str,
    history: dict,
    lock: asyncio.Lock,
    existing_exact: set[str],
    existing_stems: set[str],
):
    async with lock:
        completed_urls = set(history.get("completed_urls", []))
        completed_files = set(history.get("completed_files", []))
        completed_urls.add(url)
        if filename:
            completed_files.add(filename)
            existing_exact.add(filename)
            norm = normalize_key(filename)
            if norm:
                existing_stems.add(norm)
        history["completed_urls"] = sorted(completed_urls)
        history["completed_files"] = sorted(completed_files)
        save_history(history)


async def contagem_regressiva(segundos: int, motivo: str = "Aguardando"):
    """Imprime o tempo restante no console decrescendo a cada 1 segundo."""
    segundos = max(0, int(segundos))
    if segundos == 0:
        return
    for restante in range(segundos, 0, -1):
        print(f"\r  [{motivo}] {restante}s restantes...   ", end="", flush=True)
        await asyncio.sleep(1)
    print(f"\r  [{motivo}] Concluído!              ")


async def detectar_tempo_espera_na_pagina(game_page) -> int:
    """Lê o texto visível da página em busca de avisos de espera em segundos."""
    try:
        body_text = await game_page.locator("body").inner_text(timeout=5000)
    except Exception:
        return 0

    patterns = [
        r"(?:wait|patienter|attendre|remaining|dans|in)\s+(\d{1,3})\s*(?:seconds?|secondes?|sec\.?|s\b)",
        r"(\d{1,3})\s*(?:seconds?|secondes?)\s+(?:before|avant|left|remaining|restantes?)",
    ]
    encontrados = []
    for pat in patterns:
        for m in re.finditer(pat, body_text, re.I):
            val = int(m.group(1))
            if 1 <= val <= 300:
                encontrados.append(val)

    return max(encontrados) if encontrados else 0


async def ha_captcha_ou_bloqueio(game_page) -> bool:
    """Verifica se há indício de CAPTCHA ou verificação anti-bot na página."""
    try:
        has_widget = await game_page.locator(
            "iframe[src*='captcha' i], iframe[src*='turnstile' i], "
            ".g-recaptcha, .h-captcha, .cf-turnstile, [class*='captcha' i], [id*='captcha' i]"
        ).count()
        if has_widget > 0:
            return True

        body_text = (await game_page.locator("body").inner_text(timeout=4000)).lower()
        keywords = (
            "captcha",
            "verify you are human",
            "are you a human",
            "checking your browser",
            "anti-bot",
            "anti bot",
            "bot",
            "security check",
            "vérification",
            "humain",
        )
        return any(kw in body_text for kw in keywords)
    except Exception:
        return False


async def aguardar_resolucao_captcha_se_necessario(game_page, exigir_botao_download: bool = True):
    """Se detectar CAPTCHA/validação anti-bot, aguarda você clicar e a página avançar."""
    if not await ha_captcha_ou_bloqueio(game_page):
        return

    print("\n  [ANTI-BOT / CAPTCHA] Validação detectada! Resolva/clique no navegador e aguarde a página avançar...")
    for elapsed in range(1, MAX_CAPTCHA_WAIT_SECONDS + 1):
        captcha_still_there = await ha_captcha_ou_bloqueio(game_page)

        if exigir_botao_download:
            input_count = await game_page.locator(
                "input[type='submit'][name='download']:visible, "
                "input[type='submit'][value^='Download ' i]:visible"
            ).count()
            if input_count > 0 and not captcha_still_there:
                print(f"\r  [ANTI-BOT / CAPTCHA] Página liberada após {elapsed}s! Continuando...          ")
                await game_page.wait_for_timeout(1000)
                return
        else:
            if not captcha_still_there:
                print(f"\r  [ANTI-BOT / CAPTCHA] Validação concluída após {elapsed}s! Continuando...          ")
                await game_page.wait_for_timeout(1000)
                return

        print(f"\r  [ANTI-BOT / CAPTCHA] Aguardando seu clique / avanço da página ({elapsed}s)...   ", end="", flush=True)
        await asyncio.sleep(1)

    raise TimeoutError("Tempo limite excedido aguardando resolução manual da validação anti-bot.")


def extrair_e_remover_zip(
    zip_path: Path,
    dest_dir: Path,
    existing_exact: set[str],
    existing_stems: set[str],
) -> bool:
    """Extrai o .zip em dest_dir e apaga o arquivo .zip original se tudo correr bem."""
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            bad_file = zf.testzip()
            if bad_file is not None:
                print(f"  [ZIP] Arquivo corrompido dentro de '{zip_path.name}': {bad_file} — mantendo .zip.")
                return False

            extracted_names = zf.namelist()
            zf.extractall(dest_dir)

        for name in extracted_names:
            base = Path(name).name
            if base:
                existing_exact.add(base)
                norm = normalize_key(base)
                if norm:
                    existing_stems.add(norm)

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


async def extrair_urls_catalogo(page) -> list[str]:
    hrefs = await page.locator("a[href]").evaluate_all(
        "els => els.map(e => e.getAttribute('href')).filter(Boolean)"
    )
    current_url = page.url or LIST_URL
    urls = []
    seen = set()
    for h in hrefs:
        full_url = urljoin(current_url, h).split("#")[0]
        if is_pce_game_url(full_url) and full_url not in seen:
            seen.add(full_url)
            urls.append(full_url)
    return urls


async def coletar_links_jogos(page) -> list[str]:
    print(f"Abrindo catálogo: {LIST_URL}")
    await page.goto(LIST_URL, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(2000)

    await aguardar_resolucao_captcha_se_necessario(page, exigir_botao_download=False)
    urls = await extrair_urls_catalogo(page)

    # Se ainda estiver em validação anti-bot ou carregando, aguarda você clicar e avançar
    if not urls:
        print("\n[ANTI-BOT] Catálogo ainda não carregou os links. Se houver validação anti-bot no navegador, clique/resolva e aguarde...")
        for elapsed in range(1, MAX_CAPTCHA_WAIT_SECONDS + 1):
            print(f"\r  [ANTI-BOT / CATÁLOGO] Aguardando validação e carregamento dos jogos ({elapsed}s)...   ", end="", flush=True)
            await asyncio.sleep(1)
            urls = await extrair_urls_catalogo(page)
            if urls:
                print(f"\r  [ANTI-BOT / CATÁLOGO] {len(urls)} jogos detectados após {elapsed}s!                    ")
                break

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
        async with game_page.expect_download(timeout=60000) as page_download_info:
            await download_btn.click()
        return await page_download_info.value
    except Exception:
        for p in context.pages:
            if p != game_page:
                try:
                    async with p.expect_download(timeout=25000) as popup_download_info:
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
    history: dict,
    history_lock: asyncio.Lock,
    existing_exact: set[str],
    existing_stems: set[str],
    idx: int,
    total: int,
    url: str,
):
    url_slug_stem = normalize_key(url.rstrip("/").split("/")[-1])
    if url in set(history.get("completed_urls", [])) or (url_slug_stem and url_slug_stem in existing_stems):
        print(f"[{idx}/{total}] Já existe na pasta/histórico — pulando: {url}")
        await marcar_como_baixado(url, "", history, history_lock, existing_exact, existing_stems)
        return

    async with semaphore:
        for attempt in range(1, MAX_RETRIES + 1):
            game_page = await context.new_page()
            try:
                print(f"\n[{idx}/{total}] (Tentativa {attempt}/{MAX_RETRIES}) Abrindo: {url}")
                await game_page.goto(url, wait_until="domcontentloaded", timeout=60000)
                await game_page.wait_for_timeout(800)

                await aguardar_resolucao_captcha_se_necessario(game_page, exigir_botao_download=True)

                tempo_site = await detectar_tempo_espera_na_pagina(game_page)
                if tempo_site > 0:
                    await contagem_regressiva(tempo_site + 1, motivo="Tempo exigido pelo site")

                download_btn, btn_text = await localizar_botao_download(game_page)
                await download_btn.wait_for(state="visible", timeout=30000)

                expected_name = ""
                match = re.match(r"^\s*Download\s+(.+)$", btn_text, re.I)
                if match:
                    expected_name = sanitize_filename(match.group(1).strip())

                if ja_foi_baixado(url, expected_name, history, existing_exact, existing_stems):
                    print(f"  [{idx}/{total}] Já baixado anteriormente ({expected_name}) — pulando.")
                    await marcar_como_baixado(url, expected_name, history, history_lock, existing_exact, existing_stems)
                    return

                segundos_disabled = 0
                while await download_btn.is_disabled():
                    await aguardar_resolucao_captcha_se_necessario(game_page, exigir_botao_download=True)
                    segundos_disabled += 1
                    tempo_btn = await detectar_tempo_espera_na_pagina(game_page)
                    if tempo_btn > 0:
                        await contagem_regressiva(tempo_btn + 1, motivo="Botão bloqueado, aguardando site")
                    else:
                        print(f"\r  [Botão bloqueado] Aguardando liberação ({segundos_disabled}s)...   ", end="", flush=True)
                        await asyncio.sleep(1)
                    if segundos_disabled > 180:
                        raise TimeoutError("Botão de download permaneceu desabilitado por mais de 180s.")
                if segundos_disabled > 0:
                    print("")

                download = await disparar_e_capturar_download(context, game_page, download_btn)

                await aguardar_resolucao_captcha_se_necessario(game_page, exigir_botao_download=False)

                suggested = sanitize_filename(
                    download.suggested_filename or expected_name or f"pce_rom_{idx}.zip"
                )
                dest_path = ROMS_DIR / suggested

                if ja_foi_baixado(url, suggested, history, existing_exact, existing_stems):
                    print(f"  [{idx}/{total}] Arquivo já presente: {dest_path.name} — pulando.")
                    try:
                        await download.cancel()
                    except Exception:
                        pass
                    await marcar_como_baixado(url, suggested, history, history_lock, existing_exact, existing_stems)
                    return

                await download.save_as(str(dest_path))
                print(f"  [{idx}/{total}] Salvo com sucesso em: {dest_path}")

                if dest_path.suffix.lower() == ".zip":
                    extrair_e_remover_zip(dest_path, ROMS_DIR, existing_exact, existing_stems)
                else:
                    existing_exact.add(dest_path.name)
                    norm = normalize_key(dest_path.name)
                    if norm:
                        existing_stems.add(norm)

                await marcar_como_baixado(url, suggested, history, history_lock, existing_exact, existing_stems)

                await contagem_regressiva(MIN_DELAY_BETWEEN_DOWNLOADS, motivo="Pausa entre downloads")
                return

            except Exception as exc:
                print(f"\n  [{idx}/{total}] Falha na tentativa {attempt}: {exc}")
                await aguardar_resolucao_captcha_se_necessario(game_page, exigir_botao_download=False)
                tempo_erro = await detectar_tempo_espera_na_pagina(game_page)
                if tempo_erro > 0:
                    await contagem_regressiva(tempo_erro + 2, motivo="Limite do site atingido, aguardando")
                elif attempt < MAX_RETRIES:
                    await contagem_regressiva(10 * attempt, motivo="Aguardando para tentar novamente")
                else:
                    print(f"  [{idx}/{total}] Esgotadas as tentativas para: {url}")
            finally:
                try:
                    await game_page.close()
                except Exception:
                    pass


async def main():
    ROMS_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    history = load_history()
    history_lock = asyncio.Lock()
    existing_exact, existing_stems = coletar_arquivos_existentes_na_pasta()

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_PAGES)

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            accept_downloads=True,
        )
        list_page = await context.new_page()

        game_urls = await coletar_links_jogos(list_page)
        await list_page.close()

        total = len(game_urls)
        print(f"Total de itens encontrados: {total}")
        print(f"Arquivos já encontrados em '{ROMS_DIR.name}/': {len(existing_exact)}")
        print(f"Itens no histórico JSON: {len(history.get('completed_urls', []))}")
        print(f"Destino das ROMs: {ROMS_DIR}\n")

        if total == 0:
            await context.close()
            print("Nenhum link de ROM encontrado mesmo após aguardar. Verifique a estrutura da página.")
            return

        tasks = [
            baixar_rom(
                context,
                semaphore,
                history,
                history_lock,
                existing_exact,
                existing_stems,
                idx,
                total,
                url,
            )
            for idx, url in enumerate(game_urls, start=1)
        ]
        await asyncio.gather(*tasks)

        await context.close()
        print("\nDownload finalizado!")


if __name__ == "__main__":
    asyncio.run(main())
