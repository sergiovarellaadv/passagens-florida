"""Monitor de passagens Natal/Recife/Fortaleza -> Orlando / Miami / Tampa / Fort Lauderdale.

Roda sozinho no seu PC (Agendador de Tarefas do Windows), sem usar o Claude.
Para cada busca: abre o Google Voos, lê a aba "Melhor opção", abre "Menores
preços", espera carregar, clica nos voos mais baratos (na ida e volta, também
na volta mais barata) e lê a tela de opções de reserva. Grava o histórico,
atualiza o painel.html e avisa quando o preço cai ou chega no preço-alvo.

Em cada rodada faz todas as buscas saindo da origem principal (Natal) e uma
parte das buscas extras (outras origens e ida e volta), em rodízio.

Uso:
  python passagens.py            faz uma rodada
  python passagens.py --teste    faz só uma busca para testar
  python passagens.py --abrir    abre o navegador do monitor na tela (para
                                 resolver uma verificação do Google)
  python passagens.py --painel   só regera o painel.html
"""
import csv
import datetime as dt
import json
import os
import random
import re
import sys
import time
import traceback
import urllib.parse
import urllib.request
from pathlib import Path

PASTA = Path(__file__).resolve().parent
CONFIG = PASTA / "config.json"
HIST = PASTA / "historico.json"
ESTADO = PASTA / "estado.json"
CSV_ARQ = PASTA / "historico.csv"
PAINEL = PASTA / ("docs/index.html" if os.environ.get("CI") else "painel.html")
MODELO = PASTA / "painel_modelo.html"
EXTRATOR = PASTA / "extrator.js"
LOG = PASTA / "log.txt"
TRAVA = PASTA / ".rodando"
PERFIL = PASTA / "perfil-chrome"

CIDADES = {"NAT": "Natal", "REC": "Recife", "FOR": "Fortaleza", "MCO": "Orlando", "MIA": "Miami",
           "TPA": "Tampa", "FLL": "Fort Lauderdale"}
PADRAO = {
    "origem_principal": "NAT",
    "origens": ["NAT", "REC", "FOR"],
    "destinos": ["MCO", "MIA", "TPA", "FLL"],
    "datas": ["2026-12-26", "2026-12-27", "2026-12-28", "2026-12-29",
              "2026-12-30", "2026-12-31", "2027-01-01"],
    "volta": [],
    "extras_por_rodada": 14,
    "deslocamento": {"REC": 0, "FOR": 0},
    "preco_alvo": 3500,
    "preco_alvo_ida_volta": None,
    "queda_minima_aviso": 100,
    "ntfy_topico": "",
    "aviso_na_tela": True,
    "mostrar_navegador": False,
}

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("America/Fortaleza")
except Exception:  # sem base de fusos no Windows: usa UTC-3 fixo
    TZ = dt.timezone(dt.timedelta(hours=-3))


def agora():
    return dt.datetime.now(TZ).replace(microsecond=0)


def log(msg):
    linha = f"[{agora():%d/%m %H:%M:%S}] {msg}"
    print(linha, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(linha + "\n")
        if LOG.stat().st_size > 2_000_000:
            linhas = LOG.read_text(encoding="utf-8").splitlines()[-3000:]
            LOG.write_text("\n".join(linhas) + "\n", encoding="utf-8")
    except Exception:
        pass


def ler_json(caminho, padrao):
    try:
        return json.loads(Path(caminho).read_text(encoding="utf-8"))
    except Exception:
        return padrao


def salvar_json(caminho, dados):
    tmp = Path(str(caminho) + ".tmp")
    tmp.write_text(json.dumps(dados, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, caminho)


def config():
    c = dict(PADRAO)
    c.update(ler_json(CONFIG, {}))
    c["deslocamento"] = {**PADRAO["deslocamento"], **(c.get("deslocamento") or {})}
    if os.environ.get("NTFY_TOPICO"):
        c["ntfy_topico"] = os.environ["NTFY_TOPICO"]
    if os.environ.get("CI"):
        c["aviso_na_tela"] = False
        c["mostrar_navegador"] = False
    return c


def brl(v):
    return "R$ " + f"{int(round(v)):,}".replace(",", ".")


def dm(data):
    return f"{data[8:10]}/{data[5:7]}"


# ---------------------------------------------------------------- chaves
# Só ida:      "NAT-MCO_2026-12-26"
# Ida e volta: "NAT-MCO_2026-12-26_2027-01-10"
ANTIGA = re.compile(r"^[A-Z]{3}_\d{4}-\d\d-\d\d$")


def migrar(k):
    return "NAT-" + k if ANTIGA.match(k) else k


def partes(k):
    rota, *datas = k.split("_")
    o, d = rota.split("-")
    return o, d, datas[0], (datas[1] if len(datas) > 1 else None)


def descrever(k):
    o, d, ida, volta = partes(k)
    txt = f"{CIDADES.get(o, o)} → {CIDADES.get(d, d)} {dm(ida)}"
    return txt + (f" (volta {dm(volta)})" if volta else "")


def carregar_hist():
    hist = ler_json(HIST, [])
    for r in hist:
        if any(ANTIGA.match(k) for k in r.get("q", {})):
            r["q"] = {migrar(k): v for k, v in r["q"].items()}
    return hist


def carregar_estado():
    est = ler_json(ESTADO, {})
    est["keys"] = {migrar(k): v for k, v in (est.get("keys") or {}).items()}
    if est.get("overall") and est["overall"].get("key"):
        est["overall"]["key"] = migrar(est["overall"]["key"])
    est.setdefault("alvo", {})
    est.setdefault("rodizio", 0)
    return est


# ---------------------------------------------------------------- avisos
def avisar(titulo, texto, cfg, prioridade="default"):
    log(f"AVISO: {titulo} | {texto}")
    if cfg.get("aviso_na_tela"):
        try:
            from winotify import Notification
            Notification(app_id="Passagens Natal-Flórida", title=titulo, msg=texto[:250],
                         launch=PAINEL.as_uri()).show()
        except Exception:
            pass
    topico = (cfg.get("ntfy_topico") or "").strip()
    if topico:
        try:
            req = urllib.request.Request(
                "https://ntfy.sh/" + topico, data=texto.encode("utf-8"), method="POST",
                headers={"Title": titulo.encode("ascii", "ignore").decode() or "Passagens",
                         "Priority": prioridade, "Tags": "airplane"})
            urllib.request.urlopen(req, timeout=15).read()
        except Exception as e:
            log(f"ntfy falhou: {e}")


# ---------------------------------------------------------------- navegador
def abrir_contexto(pw, visivel):
    PERFIL.mkdir(exist_ok=True)
    args = ["--disable-blink-features=AutomationControlled", "--lang=pt-BR"]
    base = dict(user_data_dir=str(PERFIL), headless=not visivel, locale="pt-BR",
                timezone_id="America/Fortaleza", viewport={"width": 1280, "height": 900}, args=args)

    def lancar(**extra):
        if os.environ.get("CI"):
            return pw.chromium.launch_persistent_context(**base, **extra)
        try:
            return pw.chromium.launch_persistent_context(channel="chrome", **base, **extra)
        except Exception:
            log("Chrome não encontrado pelo Playwright; usando o Chromium do Playwright.")
            return pw.chromium.launch_persistent_context(**base, **extra)

    ctx = lancar()
    if not visivel:  # tira o "HeadlessChrome" do user agent
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        ua = pg.evaluate("navigator.userAgent")
        if "Headless" in ua:
            ctx.close()
            ctx = lancar(user_agent=ua.replace("HeadlessChrome", "Chrome"))
    return ctx


def recusar_cookies(page):
    """Na tela de consentimento do Google, escolhe 'Rejeitar tudo'."""
    try:
        if "consent." in page.url or page.get_by_role("button", name="Rejeitar tudo").count():
            page.get_by_role("button", name="Rejeitar tudo").first.click(timeout=5000)
            page.wait_for_timeout(2500)
    except Exception:
        pass


def url_busca(origem, destino, ida, volta=None):
    q = f"Flights to {destino} from {origem} on {ida}" + (f" through {volta}" if volta else " oneway")
    return ("https://www.google.com/travel/flights?q=" + urllib.parse.quote(q) +
            "&hl=pt-BR&curr=BRL&gl=BR")


def buscar(page, js, chave):
    o_, d_, ida, volta = partes(chave)
    url = url_busca(o_, d_, ida, volta)
    for tentativa in (1, 2):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            recusar_cookies(page)
            if "consent." in page.url:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
            o = json.loads(page.evaluate(js, bool(volta)))
            o["k"] = chave
            if o.get("err") == "BLOQUEIO":
                texto = (o.get("snip") or "").lower()
                if any(p in texto for p in ("captcha", "tráfego incomum", "unusual traffic", "robô", "robot")) \
                        or "/sorry/" in page.url:
                    return o
                if tentativa == 1:
                    continue
                return {"k": chave, "err": "FALHA"}
            return o
        except Exception as e:
            log(f"{chave}: erro na tentativa {tentativa}: {e.__class__.__name__}: {str(e)[:160]}")
            time.sleep(5)
    return {"k": chave, "err": "FALHA"}


# ---------------------------------------------------------------- o que buscar
def planejar(cfg, est, teste=False):
    hoje = agora().date().isoformat()
    datas = [d for d in cfg["datas"] if d >= hoje]
    voltas = [v for v in (cfg.get("volta") or []) if v >= hoje]
    principal = cfg["origem_principal"]
    if not datas:
        return [], [], 0
    nucleo = [f"{principal}-{d}_{ida}" for d in cfg["destinos"] for ida in datas]
    extras = [f"{o}-{d}_{ida}" for o in cfg["origens"] if o != principal for d in cfg["destinos"] for ida in datas]
    extras += [f"{principal}-{d}_{ida}_{v}" for v in voltas for d in cfg["destinos"] for ida in datas if v > ida]
    if teste:
        return [f"{principal}-MIA_{datas[0]}"], [], 0
    n = max(1, int(cfg.get("extras_por_rodada") or 0)) if extras else 0
    if not extras or n == 0:
        return nucleo, [], 0
    blocos = (len(extras) + n - 1) // n
    i = est.get("rodizio", 0) % blocos
    return nucleo, extras[i * n:(i + 1) * n], blocos


# ---------------------------------------------------------------- dados
def montar_rodada(resultados, chaves, momento):
    q, bloqueado = {}, False
    for k in chaves:
        o = resultados.get(k)
        if not o or o.get("err") or not o.get("ok"):
            if o and o.get("err") == "BLOQUEIO":
                bloqueado = True
            q[k] = {"err": (o or {}).get("err", "SEM_RESULTADO")}
            continue
        reg = o.get("reg") or [None] * 6
        cheap = o.get("cheap") or [None] * 6
        book = o.get("book") or []
        volta = o.get("volta") or [None] * 6
        fake = next((t[0] for t in (o.get("tr") or []) if len(t) > 2 and t[2] == 1), None)
        auto = cheap[0] if cheap[5] == 1 else None
        item = {"price": o.get("fin"), "conf": 1 if book else 0, "best": o.get("best"),
                "listed": reg[0], "fake": fake, "self": auto, "selfAir": cheap[1] if auto else None,
                "air": reg[1], "stops": reg[2], "times": reg[3], "dur": reg[4],
                "seller": book[0][0] if book else None}
        if o.get("rt"):
            item.update({"vAir": volta[1], "vStops": volta[2], "vTimes": volta[3], "vDur": volta[4]})
        q[k] = item
    ok = sum(1 for v in q.values() if not v.get("err"))
    status = "bloqueado" if bloqueado else ("ok" if chaves and ok == len(chaves) else "parcial")
    nota = ""
    if bloqueado:
        nota = "O Google mostrou uma verificação anti-robô; a rodada parou."
    elif status == "parcial":
        nota = f"{len(chaves) - ok} busca(s) sem resultado."
    return {"id": momento.strftime("%Y%m%d-%H%M"), "at": momento.isoformat(), "status": status,
            "note": nota, "n": ok, "total": len(chaves), "q": q}


def preco_efetivo(k, v, cfg):
    o, _, _, volta = partes(k)
    return v["price"] + (0 if volta else (cfg["deslocamento"].get(o) or 0))


def analisar(rodada, cfg, est):
    chaves = dict(est.get("keys") or {})
    geral = est.get("overall")
    quedas, alvos = [], []
    for k, v in rodada["q"].items():
        if v.get("err") or not v.get("conf") or not v.get("price"):
            continue
        antigo = (chaves.get(k) or {}).get("price")
        if antigo is None or v["price"] < antigo:
            if antigo is not None and antigo - v["price"] >= cfg["queda_minima_aviso"]:
                quedas.append((k, antigo, v["price"]))
            chaves[k] = {"price": v["price"], "at": rodada["at"]}
        rt = partes(k)[3] is not None
        alvo = cfg.get("preco_alvo_ida_volta") if rt else cfg.get("preco_alvo")
        efetivo = preco_efetivo(k, v, cfg)
        if alvo and efetivo <= alvo and efetivo < (est["alvo"].get(k) or 10 ** 9):
            alvos.append((k, efetivo))
            est["alvo"][k] = efetivo
    so_ida = [(k, v) for k, v in rodada["q"].items()
              if not v.get("err") and v.get("conf") and v.get("price") and partes(k)[3] is None]
    menor = min(so_ida, key=lambda kv: preco_efetivo(kv[0], kv[1], cfg)) if so_ida else None
    novo_geral, geral_antigo = False, geral
    if menor:
        ef = preco_efetivo(menor[0], menor[1], cfg)
        if not geral or ef < geral["price"]:
            novo_geral = geral is not None
            geral = {"key": menor[0], "price": ef, "at": rodada["at"]}
    est["keys"], est["overall"], est["updated"] = chaves, geral, rodada["at"]
    return menor, novo_geral, geral_antigo, quedas, alvos


def gravar_csv(rodada):
    novo = not CSV_ARQ.exists()
    with open(CSV_ARQ, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        if novo:
            w.writerow(["verificacao", "origem", "destino", "cidade", "data_ida", "data_volta",
                        "preco_confirmado", "confirmado", "preco_de_fora", "lista_mostrava",
                        "com_autotransferencia", "companhia", "paradas", "horario", "duracao",
                        "vendido_por", "volta_companhia", "volta_horario", "erro"])
        for k, v in rodada["q"].items():
            o, d, ida, volta = partes(k)
            w.writerow([rodada["at"], o, d, CIDADES.get(d, d), ida, volta or "", v.get("price"), v.get("conf"),
                        v.get("best"), v.get("fake"), v.get("self"), v.get("air"), v.get("stops"),
                        v.get("times"), v.get("dur"), v.get("seller"), v.get("vAir"), v.get("vTimes"),
                        v.get("err")])


def gerar_painel(rodadas, cfg):
    try:
        modelo = MODELO.read_text(encoding="utf-8")
        meta = {k: cfg[k] for k in ("origem_principal", "origens", "destinos", "datas", "volta",
                                     "deslocamento", "preco_alvo", "preco_alvo_ida_volta")}
        meta["cidades"] = CIDADES
        dados = json.dumps(rodadas, ensure_ascii=False).replace("</", "<\\/")
        html = (modelo.replace("/*__RUNS__*/[]", dados)
                .replace("/*__CFG__*/{}", json.dumps(meta, ensure_ascii=False).replace("</", "<\\/")))
        tmp = PAINEL.with_suffix(".tmp")
        tmp.write_text(html, encoding="utf-8")
        os.replace(tmp, PAINEL)
    except Exception as e:
        log(f"Não consegui gerar o painel: {e}")


# ---------------------------------------------------------------- rodada
def rodada(teste=False):
    cfg = config()
    est = carregar_estado()
    nucleo, extras, blocos = planejar(cfg, est, teste)
    chaves = nucleo + extras
    if not chaves:
        log("Todas as datas já passaram. Rode desinstalar.bat para parar o monitor.")
        return

    if TRAVA.exists() and time.time() - TRAVA.stat().st_mtime < 3 * 3600:
        log("Outra rodada ainda está em andamento; saindo.")
        return
    TRAVA.write_text(str(os.getpid()))
    inicio = agora()
    resultados = {}
    try:
        from playwright.sync_api import sync_playwright
        js = EXTRATOR.read_text(encoding="utf-8")
        log(f"Início da rodada ({len(chaves)} buscas: {len(nucleo)} principais + {len(extras)} extras"
            + (f", bloco {est.get('rodizio', 0) % blocos + 1} de {blocos}" if blocos else "") + ")")
        with sync_playwright() as pw:
            ctx = abrir_contexto(pw, cfg.get("mostrar_navegador"))
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            for i, k in enumerate(chaves):
                o = buscar(page, js, k)
                resultados[k] = o
                if o.get("err") == "BLOQUEIO":
                    log("Verificação anti-robô do Google. Rodada interrompida.")
                    break
                if o.get("err"):
                    log(f"{k}: sem resultado")
                else:
                    log(f"{k}: {brl(o['fin']) if o.get('fin') else '—'} ({o.get('s')} s)")
                if i < len(chaves) - 1:
                    time.sleep(random.uniform(4, 10))
            ctx.close()
    except Exception:
        log("Erro geral:\n" + traceback.format_exc())
    finally:
        try:
            TRAVA.unlink()
        except Exception:
            pass

    if teste:
        o = resultados.get(chaves[0], {})
        if o.get("ok"):
            log(f"TESTE OK: {descrever(chaves[0])} por {brl(o['fin'])} ({(o.get('reg') or [None, '?'])[1]}).")
        else:
            log(f"TESTE FALHOU: {o}")
        return

    r = montar_rodada(resultados, chaves, inicio)
    hist = [x for x in carregar_hist() if x.get("id") != r["id"]] + [r]
    salvar_json(HIST, hist)
    gravar_csv(r)
    if blocos:
        est["rodizio"] = est.get("rodizio", 0) + 1
    menor, novo_geral, geral_antigo, quedas, alvos = analisar(r, cfg, est)
    salvar_json(ESTADO, est)
    gerar_painel(hist, cfg)
    log(f"Fim da rodada: {r['status']}, {r['n']}/{r['total']} buscas." +
        (f" Mais barato só ida: {brl(menor[1]['price'])} {descrever(menor[0])}" if menor else ""))

    if r["status"] == "bloqueado":
        avisar("Google pediu verificação",
               "Rode abrir_navegador.bat, resolva a verificação na janela e feche-a. As próximas rodadas voltam ao normal.",
               cfg, "high")
        return
    linhas, titulo, prio = [], None, "default"
    if novo_geral and menor:
        k, v = menor
        titulo, prio = "PREÇO CAIU", "high"
        extra = f" + {brl(cfg['deslocamento'].get(partes(k)[0]))} deslocamento" if cfg["deslocamento"].get(partes(k)[0]) else ""
        linhas.append(f"{brl(v['price'])}{extra}: {descrever(k)}, {v.get('air') or ''} ({v.get('seller') or ''})")
        linhas.append(f"Antes o menor era {brl(geral_antigo['price'])}.")
    if alvos:
        titulo = titulo or "Preço-alvo atingido"
        prio = "high"
        for k, ef in sorted(alvos, key=lambda x: x[1])[:3]:
            linhas.append(f"Alvo: {brl(ef)} {descrever(k)}")
    if quedas:
        titulo = titulo or "Queda de preço"
        for k, antigo, novo in sorted(quedas, key=lambda x: x[2] - x[1])[:3]:
            linhas.append(f"{descrever(k)}: {brl(antigo)} -> {brl(novo)}")
    if titulo:
        avisar(titulo, "\n".join(linhas[:6]), cfg, prio)


def abrir_navegador():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        ctx = abrir_contexto(pw, True)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.google.com/travel/flights?hl=pt-BR&gl=BR&curr=BRL")
        print("Resolva a verificação do Google se ela aparecer e depois feche a janela do navegador.")
        try:
            while ctx.pages and not all(p.is_closed() for p in ctx.pages):
                time.sleep(1)
        except Exception:
            pass
        try:
            ctx.close()
        except Exception:
            pass


if __name__ == "__main__":
    if "--abrir" in sys.argv:
        abrir_navegador()
    elif "--painel" in sys.argv:
        gerar_painel(carregar_hist(), config())
    else:
        rodada(teste="--teste" in sys.argv)
