"""Gera histórico SIMULADO de 30 dias para o pacote energia_simulada.yaml.

Executa o mesmo modelo da casa simulada (rotina dos moradores + automações de
gestão) minuto a minuto, para o passado, e grava o resultado nas estatísticas de
longo prazo do Home Assistant. Assim o painel de energia já abre com um mês de
dados, sendo os primeiros 15 dias SEM gestão e os últimos 15 COM gestão.

Também roda um cenário de referência (linha de base): os mesmos dias, mesma
nebulosidade e mesma rotina, porém sem gestão. A diferença entre os dois é a
economia estimada, à maneira de uma M&V (medição e verificação) com linha de base.

Uso (de dentro do container, que já tem aiohttp), com o token em
config/simulacao/.token ou na variável HA_TOKEN:
    docker exec homeassistant python3 /config/simulacao/backfill.py

Pode ser executado de novo: o histórico é regravado e as somas do período ao
vivo são reajustadas para continuarem sem degrau.
"""

import asyncio
import json
import math
import os
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp

HA_URL = os.environ.get("HA_URL", "ws://localhost:8123/api/websocket")
ARQ_TOKEN = Path(__file__).parent / ".token"
TOKEN = os.environ.get("HA_TOKEN") or (ARQ_TOKEN.read_text().strip() if ARQ_TOKEN.exists() else None)
DIAS = int(os.environ.get("SIM_DIAS", "30"))
SEMENTE = int(os.environ.get("SIM_SEMENTE", "2026"))

TZ = ZoneInfo("America/Sao_Paulo")
LAT, LON = -27.7811, -54.2398  # Três de Maio/RS
DIR = Path(__file__).parent
ARQ_ESTADO = DIR / ".backfill_estado.json"
ARQ_RELATORIO = DIR / "relatorio_simulacao.md"

# Potências nominais (W) — iguais às do pacote YAML
P = {
    "chuveiro": 5500,
    "ar": 1400,
    "maquina": 500,
    "ve": 3500,
    "iluminacao": 180,
    "tv": 120,
    "tv_standby": 8,
}
LIMIAR_SOLAR = {"maquina": 600, "ve": 2500}
DURACAO_MIN = {"chuveiro": 12, "maquina": 60, "ve": 180}
ATRASO_AUSENCIA_MIN = 5
STANDBY_MIN = 5

# Estatística -> canal do modelo
CANAIS = {
    "sensor.sim_energia_importada": ("importada", "kWh"),
    "sensor.sim_energia_exportada": ("exportada", "kWh"),
    "sensor.sim_energia_solar": ("solar", "kWh"),
    "sensor.sim_energia_chuveiro": ("chuveiro", "kWh"),
    "sensor.sim_energia_ar_condicionado": ("ar", "kWh"),
    "sensor.sim_energia_maquina_lavar": ("maquina", "kWh"),
    "sensor.sim_energia_carregador_ve": ("ve", "kWh"),
    "sensor.sim_energia_iluminacao": ("iluminacao", "kWh"),
    "sensor.sim_energia_tv": ("tv", "kWh"),
    "sensor.sim_energia_geladeira": ("geladeira", "kWh"),
    "sensor.sim_custo_tarifa_branca": ("custo_branca", "BRL"),
    "sensor.sim_custo_tarifa_convencional": ("custo_convencional", "BRL"),
    "sensor.sim_credito_injecao": ("credito", "BRL"),
    "sensor.sim_importacao_posto_fora_ponta": ("imp_fora_ponta", "kWh"),
    "sensor.sim_importacao_posto_intermediario": ("imp_intermediario", "kWh"),
    "sensor.sim_importacao_posto_ponta": ("imp_ponta", "kWh"),
}


# -----------------------------------------------------------------------------
# Modelo físico
# -----------------------------------------------------------------------------
def elevacao_solar(utc: datetime) -> float:
    """Elevação solar em graus (equações da NOAA)."""
    doy = utc.timetuple().tm_yday
    h = utc.hour + utc.minute / 60
    g = 2 * math.pi / 365 * (doy - 1 + (h - 12) / 24)
    eqtime = 229.18 * (
        0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
        - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g)
    )
    decl = (
        0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g)
        - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
        - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g)
    )
    tst = h * 60 + eqtime + 4 * LON
    ha = math.radians(tst / 4 - 180)
    lat = math.radians(LAT)
    cos_zen = math.sin(lat) * math.sin(decl) + math.cos(lat) * math.cos(decl) * math.cos(ha)
    return 90 - math.degrees(math.acos(max(-1.0, min(1.0, cos_zen))))


def posto_tarifario(local: datetime, inicio_ponta: int) -> str:
    if local.isoweekday() >= 6:
        return "fora_ponta"
    if inicio_ponta <= local.hour < inicio_ponta + 3:
        return "ponta"
    if local.hour in (inicio_ponta - 1, inicio_ponta + 3):
        return "intermediario"
    return "fora_ponta"


def sorteios_do_dia(dia) -> dict:
    """Aleatoriedade do dia, idêntica para o cenário real e para a linha de base."""
    rnd = random.Random(SEMENTE * 100000 + dia.toordinal())
    tipo = rnd.random()
    if tipo < 0.5:
        nuvem = rnd.uniform(0, 0.2)
    elif tipo < 0.8:
        nuvem = rnd.uniform(0.3, 0.6)
    else:
        nuvem = rnd.uniform(0.7, 1.0)
    return {
        "nuvem_hora": [min(1, max(0, nuvem + rnd.uniform(-0.1, 0.1))) for _ in range(24)],
        "ar_tarde": rnd.random() < 0.6,
        "ar_noite": rnd.random() < 0.6,
        "luz_esquecida": rnd.random() < 0.3,
    }


def simular(inicio: datetime, fim: datetime, gestao_a_partir_de: datetime | None, prm: dict):
    """Simula minuto a minuto. Retorna lista de (hora_utc, dict canal->valor)."""
    lig = {k: False for k in ("chuveiro", "ar", "maquina", "ve", "iluminacao", "tv")}
    lig["tomada_tv"] = True
    presenca = True
    pendente = {"maquina": False, "ve": False}
    ar_desligado_gestao = False
    ligado_ha = {k: 0 for k in DURACAO_MIN}
    ausente_ha = 0
    tv_desligada_ha = 0
    ar_restaura_ha = 0
    sorteio_dia, dia_atual = None, None

    horas: list[tuple[datetime, dict]] = []
    acum: dict = {}
    hora_atual = None

    def potencias(local, utc, nuvem):
        elev = elevacao_solar(utc)
        solar = prm["kwp"] * 1000 * 0.8 * max(0.0, math.sin(math.radians(elev))) * (1 - 0.75 * nuvem)
        carga = {
            "chuveiro": P["chuveiro"] if lig["chuveiro"] else 0,
            "ar": P["ar"] if lig["ar"] else 0,
            "maquina": P["maquina"] if lig["maquina"] else 0,
            "ve": P["ve"] if lig["ve"] else 0,
            "iluminacao": P["iluminacao"] if lig["iluminacao"] else 0,
            "tv": P["tv"] if lig["tv"] else (P["tv_standby"] if lig["tomada_tv"] else 0),
            "geladeira": 150 if local.minute % 30 < 15 else 5,
        }
        consumo = sum(carga.values())
        return carga, solar, max(consumo - solar, 0), max(solar - consumo, 0)

    t = inicio
    while t < fim:
        local = t.astimezone(TZ)
        gestao = gestao_a_partir_de is not None and t >= gestao_a_partir_de
        if local.date() != dia_atual:
            dia_atual = local.date()
            sorteio_dia = sorteios_do_dia(dia_atual)
        nuvem = sorteio_dia["nuvem_hora"][local.hour]
        posto = posto_tarifario(local, prm["inicio_ponta"])
        hm = local.strftime("%H:%M")
        util = local.isoweekday() <= 5
        dsem = local.isoweekday()

        def pedir_ligar(carga):
            _, _, _, exp = potencias(local, t, nuvem)
            if gestao and posto in ("ponta", "intermediario") and exp < LIMIAR_SOLAR[carga]:
                pendente[carga] = True
            else:
                lig[carga] = True

        # --- rotina dos moradores ---
        if util and hm == "07:30":
            presenca = False
        if util and hm == "18:00":
            presenca = True
        if hm in ("06:40", "19:00"):
            lig["chuveiro"] = True
        if hm == "06:30" and util:
            lig["iluminacao"] = True
        if hm == "07:30" and not sorteio_dia["luz_esquecida"]:
            lig["iluminacao"] = False
        if hm == "18:45" and presenca:
            lig["iluminacao"] = True
        if hm == "23:30":
            lig["iluminacao"] = False
        if hm == "19:30":
            lig["tv"] = lig["tomada_tv"] = True
        if hm == "23:00":
            lig["tv"] = False
        if (hm == "19:15" and sorteio_dia["ar_noite"]) or (hm == "13:00" and dsem >= 6 and sorteio_dia["ar_tarde"]):
            lig["ar"] = True
        if hm in ("17:00", "23:00"):
            lig["ar"] = False
        if (hm == "18:30" and dsem in (2, 4)) or (hm == "10:00" and dsem == 6):
            pedir_ligar("maquina")
        if hm == "18:20" and dsem in (1, 3, 5):
            pedir_ligar("ve")

        # --- física: fim de ciclo ---
        for k, dur in DURACAO_MIN.items():
            ligado_ha[k] = ligado_ha[k] + 1 if lig[k] else 0
            if ligado_ha[k] > dur:
                lig[k] = False
                ligado_ha[k] = 0

        # --- gestão ---
        ausente_ha = 0 if presenca else ausente_ha + 1
        tv_desligada_ha = 0 if lig["tv"] else tv_desligada_ha + 1
        if gestao:
            if ausente_ha == ATRASO_AUSENCIA_MIN:
                lig["iluminacao"] = lig["tv"] = lig["ar"] = False
            if tv_desligada_ha == STANDBY_MIN and lig["tomada_tv"]:
                lig["tomada_tv"] = False
            for _ in range(3):
                _, _, imp, _ = potencias(local, t, nuvem)
                if imp <= prm["limite"]:
                    break
                if lig["ve"]:
                    lig["ve"], pendente["ve"] = False, True
                elif lig["ar"]:
                    lig["ar"], ar_desligado_gestao = False, True
                elif lig["maquina"]:
                    lig["maquina"], pendente["maquina"] = False, True
            _, _, imp, _ = potencias(local, t, nuvem)
            ar_restaura_ha = ar_restaura_ha + 1 if imp + 1500 < prm["limite"] else 0
            if ar_desligado_gestao and ar_restaura_ha >= 1:
                ar_desligado_gestao = False
                lig["ar"] = presenca
            for carga in ("maquina", "ve"):
                _, _, imp, exp = potencias(local, t, nuvem)
                cabe = imp + P[carga] < prm["limite"]
                if pendente[carga] and (exp >= LIMIAR_SOLAR[carga] or (posto == "fora_ponta" and cabe)):
                    pendente[carga], lig[carga] = False, True

        # --- medição (1 minuto) ---
        carga, solar, imp, exp = potencias(local, t, nuvem)
        hora = t.replace(minute=0, second=0, microsecond=0)
        if hora != hora_atual:
            if hora_atual is not None:
                horas.append((hora_atual, acum))
            hora_atual, acum = hora, {}

        def soma(canal, valor):
            acum[canal] = acum.get(canal, 0.0) + valor

        for k, w in carga.items():
            soma(k, w / 60000)
        soma("solar", solar / 60000)
        soma("importada", imp / 60000)
        soma("exportada", exp / 60000)
        soma("imp_" + posto, imp / 60000)
        soma("custo_branca", imp / 60000 * prm["tarifa_" + posto])
        soma("custo_convencional", imp / 60000 * prm["tarifa_convencional"])
        soma("credito", exp / 60000 * prm["valor_credito"])
        t += timedelta(minutes=1)
    if hora_atual is not None:
        horas.append((hora_atual, acum))
    return horas


def totais(horas, desde=None, ate=None):
    tot: dict = {}
    for h, v in horas:
        if (desde and h < desde) or (ate and h >= ate):
            continue
        for k, x in v.items():
            tot[k] = tot.get(k, 0.0) + x
    return tot


# -----------------------------------------------------------------------------
# WebSocket do Home Assistant
# -----------------------------------------------------------------------------
class HA:
    def __init__(self, ws):
        self.ws, self.n = ws, 0

    async def cmd(self, **msg):
        self.n += 1
        msg["id"] = self.n
        await self.ws.send_json(msg)
        while True:
            r = await self.ws.receive_json()
            if r.get("id") == self.n and r.get("type") == "result":
                if not r["success"]:
                    raise RuntimeError(f"{msg['type']}: {r['error']}")
                return r["result"]


async def primeira_linha_ao_vivo(ha: HA, ids, agora):
    """Início (hora cheia) da primeira estatística gravada pelo HA para cada id."""
    resultado = {}
    curtas = await ha.cmd(type="recorder/statistics_during_period", period="5minute",
                          start_time=(agora - timedelta(days=11)).isoformat(),
                          statistic_ids=ids, types=["sum"])
    longas = await ha.cmd(type="recorder/statistics_during_period", period="hour",
                          start_time=(agora - timedelta(days=400)).isoformat(),
                          statistic_ids=ids, types=["sum"])
    for i in ids:
        inicios = [r["start"] for r in curtas.get(i, [])[:1] + longas.get(i, [])[:1]]
        if inicios:
            primeiro = datetime.fromtimestamp(min(inicios) / 1000, timezone.utc)
            resultado[i] = primeiro.replace(minute=0, second=0, microsecond=0)
    return resultado


def preferencias_energia():
    aparelhos = [
        ("chuveiro", "Chuveiro"), ("ar_condicionado", "Ar-condicionado"),
        ("carregador_ve", "Carregador VE"), ("maquina_lavar", "Máquina de lavar"),
        ("iluminacao", "Iluminação"), ("geladeira", "Geladeira"), ("tv", "TV"),
    ]
    return {
        "energy_sources": [
            {
                "type": "grid",
                "stat_energy_from": "sensor.sim_energia_importada",
                "stat_energy_to": "sensor.sim_energia_exportada",
                "stat_cost": "sensor.sim_custo_tarifa_branca",
                "entity_energy_price": None,
                "number_energy_price": None,
                "stat_compensation": "sensor.sim_credito_injecao",
                "entity_energy_price_export": None,
                "number_energy_price_export": None,
                "power_config": {
                    "stat_rate_from": "sensor.sim_potencia_importacao",
                    "stat_rate_to": "sensor.sim_potencia_exportacao",
                },
                "cost_adjustment_day": 0,
            },
            {
                "type": "solar",
                "stat_energy_from": "sensor.sim_energia_solar",
                "stat_rate": "sensor.sim_potencia_solar",
                "config_entry_solar_forecast": None,
            },
        ],
        "device_consumption": [
            {"stat_consumption": f"sensor.sim_energia_{k}", "stat_rate": f"sensor.sim_potencia_{k}", "name": n}
            for k, n in aparelhos
        ],
    }


def brl(x):
    return f"R$ {x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def kwh(x):
    return f"{x:,.1f} kWh".replace(",", "X").replace(".", ",").replace("X", ".")


def relatorio(real, base, inicio, meio, fim, prm):
    sem = totais(base, meio, fim)
    com = totais(real, meio, fim)
    antes = totais(real, inicio, meio)

    def var(a, b):
        return f"{(b / a - 1) * 100:+.0f}%" if a else "–"

    def linha(rotulo, chave, f=kwh):
        a, b = sem.get(chave, 0.0), com.get(chave, 0.0)
        return f"| {rotulo} | {f(a)} | {f(b)} | {var(a, b)} |"

    fmt = lambda d: d.astimezone(TZ).strftime("%d/%m/%Y")
    linhas = [
        "# Relatório da simulação — DADOS FICTÍCIOS",
        "",
        "Gerado por `simulacao/backfill.py`. Todos os valores vêm de um modelo de casa",
        "simulada (rotina fixa de moradores + potências nominais típicas + sol real de",
        "Três de Maio/RS com nebulosidade sorteada). **Não são medições reais.**",
        "",
        f"- Período sem gestão: {fmt(inicio)} a {fmt(meio - timedelta(minutes=1))}",
        f"- Período com gestão: {fmt(meio)} a {fmt(fim - timedelta(minutes=1))}",
        f"- FV: {prm['kwp']} kWp · limite de demanda: {prm['limite']:.0f} W · ponta: "
        f"{prm['inicio_ponta']}h–{prm['inicio_ponta'] + 3}h (dias úteis)",
        f"- Tarifas (ilustrativas): convencional {brl(prm['tarifa_convencional'])}/kWh; branca "
        f"{brl(prm['tarifa_fora_ponta'])} / {brl(prm['tarifa_intermediario'])} / "
        f"{brl(prm['tarifa_ponta'])} por kWh; crédito de injeção {brl(prm['valor_credito'])}/kWh",
        "",
        "## Verificação (ISO 50001): período com gestão x linha de base",
        "",
        "A linha de base simula **os mesmos dias** (mesmo sol, mesma rotina) sem as",
        "automações de gestão. A diferença é a economia atribuída às automações.",
        "",
        "| Indicador | Sem gestão (linha de base) | Com gestão | Variação |",
        "|---|---:|---:|---:|",
        linha("Energia importada da rede", "importada"),
        linha("Importada no horário de ponta", "imp_ponta"),
        linha("Importada no intermediário", "imp_intermediario"),
        linha("Importada fora de ponta", "imp_fora_ponta"),
        linha("Energia injetada (excedente FV)", "exportada"),
        linha("Iluminação", "iluminacao"),
        linha("TV (inclui standby)", "tv"),
        linha("Custo da energia importada – tarifa convencional", "custo_convencional", brl),
        linha("Custo da energia importada – tarifa branca", "custo_branca", brl),
        linha("Crédito da energia injetada", "credito", brl),
        "",
        "## Escolha da modalidade tarifária (custo da energia importada)",
        "",
        "| | Tarifa convencional | Tarifa branca |",
        "|---|---:|---:|",
        f"| Sem gestão | {brl(sem['custo_convencional'])} | {brl(sem['custo_branca'])} |",
        f"| Com gestão | {brl(com['custo_convencional'])} | {brl(com['custo_branca'])} |",
        "",
        "A tarifa branca só compensa quando o consumo sai da ponta, o que depende da",
        "automação. Cargas não deslocáveis (banho às 19h) continuam pesando na ponta.",
        "Créditos de injeção estão separados porque, no SCEE (Lei 14.300/2022), eles",
        "compensam kWh e não podem zerar o custo de disponibilidade.",
        "",
        "## Primeiro período (sem gestão), valores do painel",
        "",
        f"- Importada: {kwh(antes['importada'])} (ponta: {kwh(antes.get('imp_ponta', 0))})",
        f"- Gerada FV: {kwh(antes['solar'])} · injetada: {kwh(antes['exportada'])}",
        f"- Custo tarifa branca: {brl(antes['custo_branca'])} · crédito: {brl(antes['credito'])}",
        "",
        "Os dois períodos têm dias e clima diferentes; por isso a comparação justa é a da",
        "tabela de verificação acima, contra a linha de base dos mesmos dias.",
    ]
    return "\n".join(linhas) + "\n"


async def main():
    if not TOKEN:
        sys.exit("Salve um token de longa duração em /config/simulacao/.token ou defina HA_TOKEN.")
    async with aiohttp.ClientSession() as sessao, sessao.ws_connect(HA_URL) as ws:
        await ws.receive_json()
        await ws.send_json({"type": "auth", "access_token": TOKEN})
        if (await ws.receive_json()).get("type") != "auth_ok":
            sys.exit("Token recusado pelo Home Assistant.")
        ha = HA(ws)

        estados = {s["entity_id"]: s["state"] for s in await ha.cmd(type="get_states")}

        def num(eid):
            return float(estados[f"input_number.{eid}"])

        prm = {
            "kwp": num("sim_potencia_fv"),
            "limite": num("sim_limite_demanda"),
            "inicio_ponta": int(num("sim_inicio_ponta")),
            "tarifa_convencional": num("sim_tarifa_convencional"),
            "tarifa_fora_ponta": num("sim_tarifa_fora_ponta"),
            "tarifa_intermediario": num("sim_tarifa_intermediario"),
            "tarifa_ponta": num("sim_tarifa_ponta"),
            "valor_credito": num("sim_valor_credito"),
        }

        anterior = json.loads(ARQ_ESTADO.read_text()) if ARQ_ESTADO.exists() else {}
        agora = datetime.now(timezone.utc)
        ids = list(CANAIS)
        if anterior:
            limite_id = {i: datetime.fromisoformat(anterior["limite"][i]) for i in ids}
        else:
            limite_id = await primeira_linha_ao_vivo(ha, ids, agora)
            faltando = [i for i in ids if i not in limite_id]
            if faltando:
                sys.exit("Ainda não há estatísticas para: " + ", ".join(faltando)
                         + "\nAguarde ~5 minutos após iniciar o HA com o pacote e rode de novo.")

        fim = max(limite_id.values())
        inicio_local = (fim.astimezone(TZ) - timedelta(days=DIAS)).replace(hour=0, minute=0)
        inicio = inicio_local.astimezone(timezone.utc)
        meio = (inicio_local + timedelta(days=DIAS // 2)).astimezone(timezone.utc)

        print(f"Simulando {inicio_local:%d/%m} a {fim.astimezone(TZ):%d/%m %H:%M} (gestão a partir de "
              f"{meio.astimezone(TZ):%d/%m})...")
        real = simular(inicio, fim, meio, prm)
        base = simular(inicio, fim, None, prm)

        novo_estado = {"limite": {}, "soma": {}}
        for sid, (canal, unidade) in CANAIS.items():
            soma, linhas = 0.0, []
            for hora, v in real:
                if hora >= limite_id[sid]:
                    break
                soma += v.get(canal, 0.0)
                linhas.append({"start": hora.isoformat(), "state": round(soma, 4), "sum": round(soma, 4)})
            await ha.cmd(
                type="recorder/import_statistics",
                metadata={
                    "has_sum": True,
                    "mean_type": 0,
                    "name": None,
                    "source": "recorder",
                    "statistic_id": sid,
                    "unit_class": "energy" if unidade == "kWh" else None,
                    "unit_of_measurement": unidade,
                },
                stats=linhas,
            )
            ajuste = soma - anterior.get("soma", {}).get(sid, 0.0)
            if abs(ajuste) > 1e-9:
                await ha.cmd(type="recorder/adjust_sum_statistics", statistic_id=sid,
                             start_time=limite_id[sid].isoformat(), adjustment=round(ajuste, 4),
                             adjustment_unit_of_measurement=unidade)
            novo_estado["limite"][sid] = limite_id[sid].isoformat()
            novo_estado["soma"][sid] = soma
            print(f"  {sid}: {len(linhas)} horas, total {soma:.2f} {unidade}")
        ARQ_ESTADO.write_text(json.dumps(novo_estado, indent=2))

        await ha.cmd(type="energy/save_prefs", **preferencias_energia())
        print("Painel de energia configurado.")

        texto = relatorio(real, base, inicio, meio, fim, prm)
        ARQ_RELATORIO.write_text(texto)
        print()
        print(texto)


if __name__ == "__main__":
    asyncio.run(main())
