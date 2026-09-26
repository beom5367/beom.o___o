# LTspice 회로도 자동 생성기 (`ltgen.py`)

YAML(또는 JSON)로 회로를 적으면 다음을 자동으로 만들어 줍니다.

| 출력 파일 | 내용 |
|---|---|
| `<name>.asc` | **LTspice 회로도** — LTspice에서 바로 열고 실행 가능 |
| `<name>.net` | 시뮬레이션용 넷리스트 |
| `<name>_results.txt` | **`.meas`로 지정한 값들을 뽑아낸 결과** (스윕이 있으면 표 형태) |
| `<name>.log` | 시뮬레이터 원본 로그 |

## 설치 / 실행

```bash
pip install pyyaml
python ltgen.py examples/rc_lowpass.yaml          # 회로도 + 시뮬레이션 + 결과 txt
python ltgen.py examples/rc_lowpass.yaml --no-sim # 회로도만
python ltgen.py spec.yaml -o 결과폴더 --sim ltspice
```

시뮬레이터는 자동으로 찾습니다 (`--sim auto`).

1. **LTspice** — Windows / macOS 기본 설치 경로, Linux는 wine 경로를 찾습니다.
   다른 곳에 설치했다면 환경변수로 지정하세요:
   `set LTSPICE_EXE=C:\경로\LTspice.exe`
2. **ngspice** — LTspice가 없으면 대신 사용합니다 (`.meas` 문법 차이는 자동 보정).
3. 둘 다 없으면 `.asc`만 만들고 끝나므로, LTspice에서 열어 직접 실행하면 됩니다
   (회로도 안에 `.meas` 문이 들어 있어서 **View → SPICE Error Log**에서 값을 볼 수 있습니다).

## 회로 사양 작성법

```yaml
name: rc_lowpass                 # 출력 파일 이름
title: RC Low-pass filter        # 회로도에 주석으로 표시

params:                          # (선택) .param
  Rload: 10k

components:                      # 소자 목록: nodes 순서는 아래 표 참고, 접지는 0
  - {ref: V1, type: voltage, nodes: [in, 0],   value: "PULSE(0 5 0 1n 1n 5m 10m)"}
  - {ref: R1, type: res,     nodes: [in, out], value: 1k}
  - {ref: C1, type: cap,     nodes: [out, 0],  value: 1u}
  # 선택 키: rot: R90 (회전), pos: [x, y] (직접 배치)

directives:                      # (선택) .model, .lib, .subckt 등 그대로 들어갈 줄
  - .model 2N3904 NPN(IS=1E-14 BF=300)

analysis: .tran 0 10m 0 1u       # .tran / .ac / .dc / .op (여러 개면 리스트)

sweep:                           # (선택) 파라미터 스윕 → 결과 txt가 표로 나옴
  param: Rload
  values: [1k, 5k, 10k]

measure:                         # 뽑고 싶은 값 = LTspice .meas 문법
  - TRAN vout_max MAX V(out) FROM 0 TO 5m
  - TRAN vout_at_1ms FIND V(out) AT 1m
  - TRAN t_rise TRIG V(out) VAL=0.5 RISE=1 TARG V(out) VAL=4.5 RISE=1

measure_desc:                    # (선택) txt에 같이 적힐 설명
  vout_max: 최대 출력 전압 [V]
```

### 지원 소자 (`type`)

| type | nodes 순서 | value 예 |
|---|---|---|
| `res` / `cap` / `ind` | 단자1, 단자2 | `1k`, `10u`, `1m` |
| `voltage` | +, − | `12`, `AC 1`, `SINE(0 1 1k)`, `PULSE(...)` |
| `current` | 전류가 나가는 쪽 → 들어오는 쪽 (핀1→핀2) | `1m` |
| `diode` / `zener` / `schottky` / `LED` | A, K | `1N4148` (모델 필요) |
| `npn` / `pnp` | C, B, E | `2N3904` |
| `nmos` / `pmos` | D, G, S (바디=소스) | 모델 이름 |
| `opamp` | In−, In+, OUT | 서브회로 이름 (예제 참고) |
| `bv` | +, − | `V=V(a)*2` |

그 밖의 심볼은 `symbols:` 항목으로 핀 좌표를 등록해서 쓸 수 있습니다:

```yaml
symbols:
  my_part: {sym: "MyLib\\my_part", prefix: X, pins: [[0,0],[0,64],[32,32]]}
```

> ⚠️ SPICE 단위: `m` = 밀리, `Meg` = 메가 (`1M`은 1 mΩ 입니다!)

### `.meas` 자주 쓰는 형태

| 목적 | 예 |
|---|---|
| 특정 시점 값 | `TRAN v1 FIND V(out) AT 1m` |
| 최대/최소/평균/RMS/피크투피크 | `TRAN vmax MAX V(out)` / `MIN` / `AVG` / `RMS` / `PP` |
| 상승시간 등 시간 간격 | `TRAN tr TRIG V(out) VAL=0.5 RISE=1 TARG V(out) VAL=4.5 RISE=1` |
| 조건 만족 시점 | `TRAN t_half WHEN V(out)=2.5` |
| 동작점 | `OP vout FIND V(out)` |
| AC 이득 | `AC g1k FIND mag(V(out)) AT 1k` |
| 계산식 | `TRAN ratio PARAM vout_pp/vin_pp` |

## 예제 (`examples/`)

| 파일 | 내용 | 결과 (검증값) |
|---|---|---|
| `rc_lowpass.yaml` | RC 필터 과도응답 | V(1ms)=3.16 V, 상승시간 2.197 ms (이론 2.2τ) |
| `voltage_divider_sweep.yaml` | 부하저항 스윕 → 표 형태 txt | Rload=1k → 1.000 V |
| `common_emitter_ac.yaml` | 2N3904 CE 증폭기 AC 해석 | 1 kHz 이득 ≈ 163 |
| `opamp_inverting.yaml` | 반전 증폭기 | 이득 = 10.0 |

결과 txt 예시 (`divider_sweep_results.txt`):

```
Rload	vout	iload	pload
1k	1	0.001	0.001
5k	3	0.0006	0.0018
10k	4	0.0004	0.0016
```

## 참고
- 회로도는 소자를 격자에 배치하고, 각 핀에 **넷 라벨(FLAG)** 을 붙여 연결합니다.
  같은 이름의 라벨끼리 전기적으로 연결되므로 선(wire)은 그리지 않습니다.
  보기 좋게 다듬고 싶으면 `pos`/`rot`로 위치를 정하거나 LTspice에서 옮기면 됩니다.
- 스윕이 있으면 `.asc`에는 `.step param ...` 이 들어가고(LTspice GUI용),
  자동 실행 시에는 값마다 넷리스트를 따로 만들어 돌린 뒤 표로 합칩니다.
