# Relatório da simulação — DADOS FICTÍCIOS

Gerado por `simulacao/backfill.py`. Todos os valores vêm de um modelo de casa
simulada (rotina fixa de moradores + potências nominais típicas + sol real de
Três de Maio/RS com nebulosidade sorteada). **Não são medições reais.**

- Período sem gestão: 21/09/2026 a 05/10/2026
- Período com gestão: 06/10/2026 a 21/10/2026
- FV: 2.5 kWp · limite de demanda: 6000 W · ponta: 18h–21h (dias úteis)
- Tarifas (ilustrativas): convencional R$ 0,80/kWh; branca R$ 0,68 / R$ 1,00 / R$ 1,52 por kWh; crédito de injeção R$ 0,60/kWh

## Verificação (ISO 50001): período com gestão x linha de base

A linha de base simula **os mesmos dias** (mesmo sol, mesma rotina) sem as
automações de gestão. A diferença é a economia atribuída às automações.

| Indicador | Sem gestão (linha de base) | Com gestão | Variação |
|---|---:|---:|---:|
| Energia importada da rede | 192,4 kWh | 185,5 kWh | -4% |
| Importada no horário de ponta | 104,0 kWh | 39,3 kWh | -62% |
| Importada no intermediário | 21,0 kWh | 14,0 kWh | -34% |
| Importada fora de ponta | 67,4 kWh | 132,3 kWh | +96% |
| Energia injetada (excedente FV) | 140,6 kWh | 147,7 kWh | +5% |
| Iluminação | 21,3 kWh | 15,2 kWh | -28% |
| TV (inclui standby) | 9,0 kWh | 6,5 kWh | -27% |
| Custo da energia importada – tarifa convencional | R$ 153,94 | R$ 148,42 | -4% |
| Custo da energia importada – tarifa branca | R$ 224,95 | R$ 163,65 | -27% |
| Crédito da energia injetada | R$ 84,38 | R$ 88,60 | +5% |

## Escolha da modalidade tarifária (custo da energia importada)

| | Tarifa convencional | Tarifa branca |
|---|---:|---:|
| Sem gestão | R$ 153,94 | R$ 224,95 |
| Com gestão | R$ 148,42 | R$ 163,65 |

A tarifa branca só compensa quando o consumo sai da ponta, o que depende da
automação. Cargas não deslocáveis (banho às 19h) continuam pesando na ponta.
Créditos de injeção estão separados porque, no SCEE (Lei 14.300/2022), eles
compensam kWh e não podem zerar o custo de disponibilidade.

## Primeiro período (sem gestão), valores do painel

- Importada: 187,7 kWh (ponta: 101,0 kWh)
- Gerada FV: 153,4 kWh · injetada: 122,4 kWh
- Custo tarifa branca: R$ 218,74 · crédito: R$ 73,46

Os dois períodos têm dias e clima diferentes; por isso a comparação justa é a da
tabela de verificação acima, contra a linha de base dos mesmos dias.
