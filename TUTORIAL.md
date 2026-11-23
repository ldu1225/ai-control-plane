# AI Control Plane — 원클릭 설치 (One-Click Setup)

이 가이드는 **AI Control Plane**을 Google Cloud 프로젝트에 자동으로 배포합니다.
API 활성화, 전용 서비스 계정과 IAM 역할, Cloud Storage 설정 저장소, Secret Manager 키, BigQuery 감사 로그 데이터셋 및 싱크, Data Access 감사 로그, Gemini Enterprise 사용 로그(프롬프트 로깅 포함), Antigravity 사용자별 한도 집행 일정(Cloud Scheduler)과 실시간 집행(Pub/Sub), 일일 공개 단가·환율 확인, Cloud Run 서비스까지 **한 번에 자동 구성**됩니다.

---

## Step 1. 프로젝트 선택

<walkthrough-project-setup billing="true"></walkthrough-project-setup>

필요한 것:

- 선택한 프로젝트의 **소유자(roles/owner)** 권한, 또는 편집자 + 프로젝트 IAM 관리자 권한
- 결제 계정이 연결된 프로젝트 (설치 스크립트가 확인하고, 가능하면 자동으로 연결합니다)
- 조직(Organization)은 없어도 됩니다. 거부 정책으로 집행하는 옵션(`--enforcement-backend iam_deny`)을 쓸 때만 조직이 필요합니다.

---

## Step 2. 원클릭 설치 실행

아래 명령은 Step 1에서 고른 프로젝트에 배포합니다. 처음 실행할 때는 컨테이너 이미지를 빌드하므로 시간이 조금 걸립니다.

```bash
./scripts/install.sh --project {{project-id}}
```

> **팁 — 다른 방법으로 실행하기**
> - **질문에 답하면서 설치** (프로젝트 번호, 함께 볼 프로젝트, 리전, 관리자, 뷰어를 차례로 묻습니다. Enter는 기본값):
>   ```bash
>   ./scripts/install.sh
>   ```
> - **여러 프로젝트를 하나의 대시보드로 통합 관제 (권장)**:
>   ```bash
>   ./scripts/install.sh --project hub-project-id --link-projects spoke-project-1,spoke-project-2
>   ```
> - **프로젝트마다 별도 대시보드 개별 배포**:
>   ```bash
>   ./scripts/install.sh --projects my-project-1,my-project-2
>   ```
> - **무엇이 바뀌는지만 미리 보기** (아무것도 만들지 않음): 명령 끝에 `--dry-run`
> - **조회 전용 설치** (IAM 쓰기 권한 없이 대시보드와 시뮬레이션만): `--read-only`
> - **거부 정책으로 Antigravity 한도 집행** (조직 필요, 전용 Cloud Run 작업이 적용): `--enforcement-backend iam_deny`
> - 전체 옵션: `./scripts/install.sh --help`, `./scripts/deploy.sh --help`

---

## Step 3. 접속 확인

배포가 끝나면 터미널 하단의 요약을 확인합니다.

- `Dashboard URL :` 줄의 `https://ai-control-plane-...run.app/login` 주소를 열어 Google 계정으로 로그인합니다.
- 조직 정책 때문에 Google 로그인(IAP)을 쓸 수 없는 프로젝트에서는 ID/비밀번호 로그인으로 자동 전환됩니다. `Admin password:` 줄의 명령으로 기본 계정 `admin`의 초기 비밀번호를 확인하고, 로그인 후 설정 > 접근 제어에서 바꾸세요.
- Antigravity 사용량을 사용자별로 집계하려면 Google Cloud 콘솔 > Gemini Enterprise > 설정 > AI developer tools > Compliance > **Metadata logging**을 켜야 합니다 (기본값은 꺼짐). 요약에 경고가 나오면 이 설정을 확인하세요.

---

## 다시 실행하기 (업데이트)

같은 명령을 다시 실행하면 기존 배포를 그대로 업데이트합니다. 이번에 지정한 옵션은 `deploy.{{project-id}}.env` 파일에 저장되므로, 다음 실행에서 옵션을 생략해도 리전, 관리자, 접근 방식 같은 설정이 유지됩니다. 바꾸고 싶은 값만 옵션으로 다시 주면 됩니다.

---

## 삭제하기

대시보드가 만든 리소스를 지웁니다. 한도 때문에 정지된 사용자는 먼저 원래 권한을 돌려받습니다.

```bash
./scripts/teardown.sh --project {{project-id}}
```

- 감사 로그가 담긴 BigQuery 데이터셋과 Artifact Registry 저장소는 남깁니다 (삭제 명령을 출력합니다).
- 설정 버킷과 로그 싱크까지 지우려면 `--delete-data`, 아무도 쓰지 않는 사용자 정의 역할까지 지우려면 `--delete-roles`를 붙입니다.
- 먼저 `--dry-run`으로 지울 목록을 확인할 수 있습니다.

<walkthrough-conclusion-trophy></walkthrough-conclusion-trophy>
