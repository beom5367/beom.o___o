# LTspice 회로도 자동 생성기

YAML 파일에 회로를 적으면 **LTspice 회로도(.asc)** 와 **측정값(.txt)** 을 만들어 zip 하나로 묶어 줍니다.

```
python ltgen.py 예제.yaml
```

```
output/예제.zip
└─ 예제/
   ├─ RC필터.asc
   ├─ 분압기.asc
   ├─ CE증폭기.asc
   ├─ 반전증폭기.asc
   └─ 측정결과.txt
```

- 소자는 **와이어로 연결**됩니다. 전원(VCC)은 위, 접지는 아래 레일로 자동 배치됩니다.
- `.meas` 에 쓰인 넷(예: `V(out)`)에만 이름표가 한 개 붙습니다. 그래야 LTspice에서 측정이 됩니다.
- 저항·커패시터·인덕터는 **전류가 양수로 나오도록** 핀 방향을 자동으로 맞춥니다.
  (다이오드·트랜지스터·전원은 소자 극성 그대로입니다.)
- 생성된 회로도는 다시 읽어서 연결이 사양과 똑같은지 검사한 뒤 저장합니다.

## 준비

```
pip install pyyaml
```

측정값을 뽑으려면 시뮬레이터가 하나 필요합니다.
- **LTspice**: 기본 설치 경로에 있으면 자동으로 찾습니다. 다른 곳이면 `LTSPICE_EXE` 환경변수로 경로를 지정하세요.
- **ngspice**: LTspice가 없을 때 대신 씁니다. 전류 방향 자동 정리도 ngspice로 합니다.

둘 다 없으면 회로도만 만듭니다(`--no-sim` 과 같음).

## 회로 적는 법

```yaml
project: 실험3            # zip / 폴더 이름
circuits:
  - name: RC필터          # .asc 파일 이름
    components:           # "이름 노드... 값"  (접지는 0)
      - V1 in 0 PULSE(0 5 0 1n 1n 5m 10m)
      - R1 in out 1k
      - C1 out 0 1u
    analysis: .tran 0 10m 0 1u
    measure:              # LTspice .meas 문법
      - TRAN vout_1ms FIND V(out) AT 1m
      - TRAN i_r1_avg AVG I(R1) FROM 0 TO 5m
```

소자 종류는 이름 첫 글자로 정해집니다.

| 첫 글자 | 소자 | 노드 순서 |
|---|---|---|
| R / C / L | 저항 / 커패시터 / 인덕터 | 두 단자 |
| V / I | 전압원 / 전류원 | +, − |
| D | 다이오드 | A, K |
| Q | NPN 트랜지스터 | C, B, E |
| M | NMOS | D, G, S |
| U, X | 이상적 opamp | −입력, +입력, 출력 |
| B | 행동 전압원 (`V=...`) | +, − |

다른 종류는 `이름:종류` 로 씁니다: `Q2:pnp c b e 2N3906`, `M2:pmos d g s PM1`,
`D1:zener 0 out BZX5V1`, `D2:LED a k LED1`, `D3:schottky a k 1N5817`.

### 그 밖의 항목

| 항목 | 설명 |
|---|---|
| `directives` | `.model`, `.lib`, `.subckt` 등 그대로 들어갈 줄 |
| `params` | `.param` 값 (`{Rload: 10k}`) |
| `sweep` | 값을 바꿔 가며 반복 → 결과가 표로 나옴 (`{param: Rload, values: [1k, 10k]}`) |
| `labels` | 와이어 대신 이름표로 연결할 넷 (`[out, vcc]`) |
| `wiring: labels` | 모든 넷을 이름표로 연결 |
| `show` | 측정에 안 쓰여도 이름표를 붙일 넷 |
| `wires` | 직접 그릴 와이어 (32 단위 격자 좌표 목록). 나머지 연결은 자동 |
| `gnd` | 접지 기호를 둘 위치 (없으면 가장 아래 와이어). 접지는 와이어로 조금 내린 뒤 붙습니다 |
| `sim_only` | 회로도에는 넣지 않고 측정값 계산에만 쓰는 줄 (예: 다이오드 `.model`) |

소자를 사전(dict) 형식으로 쓰면 위치도 지정할 수 있습니다.

```yaml
      - {ref: R2, type: res, nodes: [n, out], value: 10k, dir: right, at: [10, 2]}
```

- `dir`: `down`, `up`, `right`, `left` (1번 핀에서 2번 핀으로 전류가 흐르는 방향). 트랜지스터·opamp는 `flip: true` 로 좌우를 뒤집습니다.
- `at`: 1번 핀 위치 (32 단위 격자)
- `keep: true`: 전류 방향 자동 정리를 끕니다.

### 매뉴얼 그림과 똑같이 그리기

모든 소자에 `at` 과 `dir` 을 주고 `wires` 로 선을 그리면 자동 배치 없이 그대로 그립니다.
`Lab2.yaml` (정류기 실험 Fig 1~3) 이 그 예입니다.

```yaml
    components:
      - {ref: V1, nodes: [a, 0], value: SINE(0 10 60), dir: down, at: [0, 2]}
      - {ref: D1, nodes: [a, vout], value: 1N4004, dir: right, at: [3, 0]}
      - {ref: R1, nodes: [vout, 0], value: 1k, dir: down, at: [8, 2]}
    wires:
      - [[0, 2], [0, 0], [3, 0]]
      - [[5, 0], [8, 0], [8, 2]]
      - [[0, 5], [0, 7], [8, 7], [8, 5]]
    gnd: [8, 7]
```

단자 간격(32 단위): 저항·인덕터·전원 3칸, 커패시터·다이오드 2칸.

SPICE 단위: `m` = 밀리, `Meg` = 메가 (`1M` 은 1 mΩ 입니다).

### 자주 쓰는 `.meas`

| 목적 | 예 |
|---|---|
| 특정 시점 값 | `TRAN v1 FIND V(out) AT 1m` |
| 최대 / 최소 / 평균 / RMS / 피크투피크 | `TRAN vmax MAX V(out)` (`MIN`, `AVG`, `RMS`, `PP`) |
| 상승 시간 | `TRAN tr TRIG V(out) VAL=0.5 RISE=1 TARG V(out) VAL=4.5 RISE=1` |
| 조건을 만족하는 시각 | `TRAN t_half WHEN V(out)=2.5` |
| 동작점 | `OP vout FIND V(out)` |
| 트랜지스터 전류 | `OP icq FIND Ic(Q1)` |
| AC 이득 | `AC g1k FIND mag(V(out)) AT 1k` |
| 계산식 | `TRAN gain PARAM vout_pp/vin_pp` |

## 명령 옵션

```
python ltgen.py 회로.yaml -o 폴더     # 출력 위치 (기본 output)
python ltgen.py 회로.yaml --no-sim    # 회로도만
python ltgen.py 회로.yaml --sim ltspice | ngspice
```
