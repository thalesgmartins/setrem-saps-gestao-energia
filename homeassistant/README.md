# Prova de conceito: gestão de energia no Home Assistant (dados simulados)

Demonstração prática do artigo *Automação residencial e predial como meio de gestão
da energia elétrica* (SAPS/SETREM). O artigo é teórico e conclui sugerindo "um estudo
prático que quantifique a economia". Esta prova de conceito mostra **como** esse estudo
seria conduzido, usando uma casa **simulada** no lugar de medidores reais.

> **Na banca:** todos os sensores têm o prefixo `sim_` e o rótulo "(simulado)".
> Os aparelhos são relés virtuais e as potências são valores nominais típicos.
> O que é real é a plataforma: os mesmos sensores, automações e painéis funcionariam
> com medidores físicos (ex.: Shelly EM no quadro, tomadas Zigbee com medição) bastando
> trocar a fonte de cada sensor.

## O que foi montado

| Etapa ISO 50001 | No Home Assistant | Arquivo |
|---|---|---|
| **Medir** | Potência (W) de 7 cargas, FV, importação e injeção; energia (kWh) por integração de Riemann | `config/packages/energia_simulada.yaml` |
| **Analisar** | Posto tarifário da tarifa branca, tarifa vigente, custo branca x convencional, kWh por posto (`utility_meter`) | idem |
| **Agir** | 5 automações "Gestão: …" (ver abaixo), registradas no logbook | idem |
| **Verificar** | Painel de energia com 30 dias de histórico + relatório contra linha de base | `config/simulacao/backfill.py` |

Casa simulada: chuveiro 5,5 kW, ar-condicionado 1,4 kW, carregador de VE 3,5 kW,
máquina de lavar 0,5 kW, iluminação 180 W, TV 120 W (8 W em standby), geladeira
ciclando, FV de 2,5 kWp com a posição real do sol em Três de Maio/RS.

Automações de gestão (só atuam com **Gestão de energia ativa** ligada):

1. **Deslocamento de carga**: VE e máquina ligados na ponta/intermediário são adiados.
2. **Liberação**: a carga adiada volta quando o posto vira fora de ponta (se couber no
   limite de demanda) ou quando há excedente solar suficiente (autoconsumo FV).
3. **Controle de demanda**: acima do limite (6 kW), desliga VE → ar → máquina.
   O ar é religado quando a demanda cai.
4. **Ausência**: sem presença, desliga iluminação, TV e ar.
5. **Standby**: corta a tomada da TV 5 min após desligada.

A rotina dos moradores (banho 6h40/19h, chegada 18h, VE às 18h20 seg/qua/sex, etc.)
também é automatizada, para a casa "viver" sozinha. Desligue **Rotina simulada dos
moradores** para controlar tudo manualmente.

## Como usar

1. Os arquivos já estão em `config/`; o `configuration.yaml` carrega `packages/` e
   o dashboard **Energia (simulado)** na barra lateral.
2. Execute o script **Demo – Restaurar simulação** uma vez (liga gestão, rotina e presença).
3. Gere o histórico de 30 dias e configure o painel de energia:
   - No HA: Perfil → Segurança → *Tokens de acesso de longa duração* → criar.
   - Salve o token em `config/simulacao/.token` (já está no `.gitignore`).
   - `docker exec homeassistant python3 /config/simulacao/backfill.py`
   - O script imprime e salva `config/simulacao/relatorio_simulacao.md`.

Os primeiros 15 dias do histórico são **sem gestão** e os últimos 15 **com gestão**.
O relatório compara o período com gestão contra uma **linha de base**: os mesmos dias,
com o mesmo sol e a mesma rotina, porém sem as automações. Assim a economia não é
confundida com diferença de clima (é a lógica de M&V usada em eficiência energética).
O script pode ser rodado de novo, por exemplo após mudar as tarifas.

## Roteiro sugerido para a banca (≈ 5 min, funciona a qualquer hora)

Dashboard **Energia (simulado)** → aba **Demonstração**:

1. **Chegada na ponta**: liga luz, TV, ar, chuveiro, VE e máquina às "18h".
   *Mostrar:* o chuveiro estoura o limite → o ar é desligado; VE e máquina são adiados
   (logbook "o que a gestão fez"). No gráfico, a importação fica abaixo do limite.
2. **Fim do banho e da ponta**: o ar volta em ~1 min; ao virar fora de ponta,
   máquina e VE são liberados.
3. **Saída de casa**: luz e TV ligadas sem ninguém → desligadas em 1 min;
   5 min depois a tomada da TV corta o standby.
4. **Excedente solar**: VE e máquina adiados; o "sol" aparece e o excedente libera as
   cargas (autoconsumo FV).
5. Aba **Histórico**: painel de energia com 30 dias; comparar uma semana do início com
   uma do fim e o gráfico de kWh por posto tarifário. Fechar com a tabela do relatório.

Para comparar "com x sem automação" ao vivo, repita um cenário com **Gestão de energia
ativa** desligada.

## Limitações (para declarar)

- Dados fictícios: potências nominais, rotina fixa, nebulosidade sorteada.
- Tarifas são ilustrativas; ajuste os `input_number` à tabela vigente da RGE e confira
  o horário de ponta da distribuidora (`Início do horário de ponta`).
- Feriados (fora de ponta o dia inteiro na tarifa branca) não são tratados.
- Crédito de injeção em R$/kWh é uma simplificação do SCEE (Lei 14.300/2022).
- Os ganhos medidos dependem do modelo; o valor do trabalho está no método
  (medir → analisar → agir → verificar), não no percentual em si.
