# MSSA 총무 업무 효율화를 위한 AI 일정 조율 실험

Claude Opus 5.5와 Sonnet 5.5가 일정 조율의 조건·승인·도구 오류를 어떻게 처리하는지 비교했습니다.
MSSA 총무 업무에서 출발해 합성 사례와 모의 캘린더로 실행한 개인 프로젝트입니다.

## 주요 결과

| 지표 | Opus 5.5 | Sonnet 5.5 |
|---|---:|---:|
| 실제 본실험 실행 | 24회 | 24회 |
| 원래 자동 채점 통과 | 21/24 (87.5%) | 24/24 (100%) |
| 업무 결과 일치 · 사후 검토 | 24/24 | 24/24 |
| 평균 전체 처리 시간 | 40.1초 | 36.1초 |
| 모델 판단 / CLI 호출 | 144회 | 146회 |

**결과는 동률이며, Sonnet의 평균 처리 시간은 약 10.0% 짧았습니다.**
자동 점수 차이 3회는 Opus가 A·B만으로 불가능함을 증명한 뒤 C 조회를 생략한 데서 발생했습니다.
전원 조회를 요구한 원래 채점과 업무 결과를 분리해 보고하며 원래 점수를 덮어쓰지 않았습니다.

## 실험 설계

기본 조율·조건 변경·실행 승인·오류 복구 4개 유형 × 각 2사례 × 2모델 × 3반복 = 48회.
A/B/C는 같은 회의에 참석하는 세 명의 가상 참여자입니다.
Claude Code 2.1.330, Python 3.14.6, effort medium을 사용했습니다.
정정 발언은 최초 입력에 모두 주었고, 실제 캘린더 API는 사용하지 않았습니다.

## 보고서와 자료

- [보고서 PDF · 10쪽](schedule_experiment_report.pdf)
- [보고서 HTML](schedule_experiment_report.html): 파일을 다운로드한 뒤 브라우저로 열면 그래프와 자료 다운로드를 사용할 수 있습니다.
- [실험 자료 전체 ZIP](schedule_experiment_files_public.zip)
- [공개용 실행 로그](results_public.zip): 본실험 48회·290판단과 연결 확인 기록.
- [실행별 검토 CSV](audited_trials.csv) / [원래 자동 채점 CSV](summary.csv)
- [공개 사본 처리 내역](PUBLICATION_NOTE.md)

GitHub 화면의 Code → Download ZIP으로 이 저장소 전체를 내려받을 수도 있습니다.

## 새 실험 실행

Claude Code에 로그인한 뒤 Anaconda Prompt 또는 터미널에서 실행합니다.

```sh
python experiment.py --models claude-opus-5-5 claude-sonnet-5-5 --out runs/new_run
```

새 실험은 연결된 Claude 계정의 사용량을 소모합니다. Python 실행기는 표준 라이브러리만 사용합니다.
기존 결과를 확인하는 데는 모델 호출이나 로그인 없이 아래 재검증 명령만 사용하면 됩니다.

## 기존 로그 재검증

저장소의 최상위 폴더에서 실행합니다. 실제 모델을 새로 호출하지 않습니다.

```sh
python analyze_results.py --input results_public.zip --runner experiment.py --out rechecked
```

48개 결과의 채점, 290개 판단의 입력·응답·상태 이력과 모델 ID를 대조합니다.
한국어 최종 응답은 ChatGPT가 검토했으며 독립적인 사람의 전수 검수는 아직 하지 않았습니다.
실행기의 단위 검증 28회는 본실험 48회와 별도입니다.

## AI 협업과 책임

- 사람: 업무 목표·모델·실행 조건 선택, 로컬 실행, 최종 적용 판단과 책임.
- ChatGPT: 설계·코드·오류 해결 지원, 기록 대조·해석·보고서 작성.
- Python 실행기: 반복 호출, 모의 도구 실행, 저장·자동 채점·기록.
- Claude Code: 실제 모델 응답과 다음 JSON 행동 생성.

ChatGPT가 실험 중 사용자의 PC를 직접 제어한 것은 아닙니다.

## 해석 한계

48회는 8개 합성 상황의 반복으로 일반 업무 성공률을 뜻하지 않습니다.
두 모델은 같은 5.5 세대이며 이 실험은 신·구 세대 개선율 비교가 아닙니다.
과제가 두 모델의 능력 차이를 드러내기에는 쉬웠을 가능성이 있습니다.
시간에는 CLI 시작·통신·모델 응답 등이 포함돼 순수 추론 속도나 통계적 우월성을 확정할 수 없습니다.
서버 부하, 실제 업무 시간 절감, 구독 비용은 별도로 측정하지 않았습니다.

## 모델 정보 출처

- [Anthropic: Sonnet 5.5 소개](https://www.anthropic.com/claude-sonnet-5-5)
- [Anthropic: Opus 5.5 모델 개요](https://platform.claude.com/en/models/opus-5-5/overview)

공식 모델 정보 확인일: 2026-11-23. 실험 결과의 근거는 저장소의 실행 기록입니다.
