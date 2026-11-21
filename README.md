# emmu-pce-roms

Scripts de automação em Python + **Playwright** usados para coletar ROMs de **PC Engine / TurboGrafx-16 (PCE)** no catálogo de `planetemu.net`, descompactá-las e consolidá-las em `roms/`.

> Este README documenta a **proveniência real** do conteúdo e o funcionamento exato do script. Leia os avisos abaixo antes de tornar o repositório público, fazer fork ou redistribuir.

---

## Avisos legais e proveniência — leia antes de publicar

### De onde vieram as ROMs deste repositório

- **Fonte:** `https://www.planetemu.net/en/roms/nec-pc-engine` — catálogo **público e online**, acessado pelo script `script.py` deste repositório. No momento da coleta o site estava no ar e oferecia download direto dos arquivos.
- **Como foi feito:** o script abre o catálogo, extrai todos os links `https://www.planetemu.net/en/rom/nec-pc-engine/...`, visita cada página de jogo, resolve/aguarda validações anti-bot manualmente quando aparecem, clica no botão `Download ...` e salva o arquivo em `roms/`.
- **Extensões presentes em `roms/`:** `.pce` (113 arquivos), incluindo imagens de BIOS.
- **Controle de duplicatas:** o arquivo `downloaded_roms.json` registra URLs e nomes já processados, e o script também verifica os arquivos existentes em `roms/` antes de baixar novamente.

### O que isso significa — e o que não significa

- O site de origem é público e estava online: isso é um **fato de proveniência**, não uma autorização de redistribuição. **Disponível publicamente não é o mesmo que domínio público**, nem implica licença de uso.
- Os direitos autorais e as marcas dos jogos pertencem aos **titulares originais** (NEC, Hudson Soft, Konami, Namco etc.), **não** ao site de download e não a este repositório.
- As ROMs em `roms/` **não são cobertas pela GNU GPL-3.0** deste projeto. A licença em `LICENSE` aplica-se **exclusivamente ao código do script** (`script.py`). Declarar o contrário seria atribuir a si autoria de obras de terceiros.

### Responsabilidade do usuário

- Antes de publicar, confirme que possui base legal para cada arquivo: backup de mídia que você possui (nos termos da lei do seu país), *homebrew*, conteúdo em domínio público ou autorização expressa do titular. Em caso de dúvida, **remova as ROMs e mantenha apenas o script**.
- Nenhum texto deste README renuncia, limita ou transfere direitos de terceiros, e nenhum disclaimer garante imunidade jurídica. A responsabilidade pelo conteúdo publicado é do mantenedor do repositório.
- Uso pretendido: estudo de emulação, testes e preservação pessoal, sempre dentro da legalidade local.

### Automação responsável

- O script interage com um serviço de terceiros e detecta CAPTCHA/verificação anti-bot. Respeite termos de uso e limites do site: ele roda com **uma** página concorrente e pausa de **3 segundos** entre downloads justamente para reduzir carga.
- Código fornecido *"as is"*, sem garantia de funcionamento contínuo: sites mudam seletores, layout e políticas de download.

---

## Estrutura do repositório

```text
emmu-pce-roms/
├── script.py             # Coleta, baixa, extrai e registra o histórico
├── downloaded_roms.json  # Histórico de URLs/nomes já processados
├── .browser_profile/     # Perfil persistente do Chromium (gerado em tempo de execução)
├── LICENSE               # GNU GPL-3.0 — aplicável somente ao script
├── README.md             # Esta documentação
└── roms/                 # ROMs coletadas e extraídas
```

## Pré-requisitos

- Python 3.10+ (`python --version`)
- Playwright: `pip install playwright`
- Chromium para o Playwright: `playwright install chromium`
- Opcional em Linux: `playwright install-deps chromium`

## Como as ROMs foram obtidas

### `script.py` — coleta e pós-processamento em um único fluxo

Configuração atual do arquivo:

| Variável | Valor | Finalidade |
| --- | --- | --- |
| `LIST_URL` | `https://www.planetemu.net/en/roms/nec-pc-engine` | Catálogo percorrido |
| `ROMS_DIR` | `BASE_DIR / "roms"` | Destino final dos arquivos |
| `HISTORY_FILE` | `BASE_DIR / "downloaded_roms.json"` | Evita rebaixar o que já foi processado |
| `PROFILE_DIR` | `BASE_DIR / ".browser_profile"` | Perfil persistente do navegador |
| `MAX_CONCURRENT_PAGES` | `1` | Uma aba de download por vez |
| `MIN_DELAY_BETWEEN_DOWNLOADS` | `3` segundos | Pausa entre downloads |
| `MAX_CAPTCHA_WAIT_SECONDS` | `600` | Tempo limite para resolução manual do desafio |
| `MAX_RETRIES` | `3` | Tentativas por ROM |

Fluxo executado:

1. Abre o catálogo em um contexto persistente do Chromium (`headless=False`).
2. Extrai os links `/en/rom/nec-pc-engine/...` com `locator("a[href]").evaluate_all`, normaliza com `urljoin` e deduplica.
3. Para cada jogo, abre a página, detecta CAPTCHA/anti-bot e aguarda a resolução manual no navegador; também respeita contagens regressivas exibidas pelo site e botões temporariamente desabilitados.
4. Localiza o `input[type='submit'][name='download']` (ou link/botão `Download ...`), captura o download na mesma aba ou em popup e salva em `roms/`.
5. Se o arquivo for `.zip`, valida com `testzip()`, extrai para `roms/` e apaga o ZIP original; caso contrário, registra o arquivo diretamente.
6. Atualiza `downloaded_roms.json` com a URL e o nome baixados, usando `asyncio.Lock` para evitar corridas.

## Execução

```bash
git clone <URL-DO-REPOSITORIO> emmu-pce-roms
cd emmu-pce-roms
python -m venv .venv
source .venv/bin/activate          # Windows: .\.venv\Scripts\Activate.ps1
pip install playwright
playwright install chromium
python script.py
```

> Durante a execução, mantenha o navegador aberto: quando aparecer validação anti-bot, o script espera até você resolvê-la (máximo de 10 minutos por desafio).

## Solução de problemas

- **Validação anti-bot frequente:** resolva no navegador visível; aumente `MIN_DELAY_BETWEEN_DOWNLOADS` se os bloqueios persistirem.
- **Nenhum link encontrado:** o site pode ter mudado a estrutura das URLs; verifique `is_pce_game_url()` e `extrair_urls_catalogo()`.
- **Arquivo ZIP corrompido:** o script mantém o `.zip` original e registra o erro no console; remova manualmente se necessário.
- **Rebaixando arquivos:** confira `downloaded_roms.json` e os nomes já presentes em `roms/` — a deduplicação usa nomes normalizados (sem acentos/pontuação).
- **Publicação no GitHub:** se você não tiver base legal para as ROMs, adicione os formatos ao `.gitignore` e remova os arquivos do histórico do Git antes de publicar.

## Licença

- **Script:** GNU General Public License v3.0 — texto completo em `LICENSE`.
- **ROMs:** não licenciadas por este repositório. Consulte a seção de avisos.
- Contribuições e correções são bem-vindas via issues ou pull requests, restritas ao código do script.
