# AI Control Plane — 원클릭 설치 (One-Click Setup)

이 가이드는 **AI Control Plane**을 Google Cloud 프로젝트에 자동으로 배포합니다.
API 활성화, 전용 서비스 계정, IAM 역할, Cloud Storage 설정 저장소, Secret Manager 키, BigQuery 감사 로그 데이터셋 및 싱크, Data Access 감사 로그, Gemini Enterprise 프롬프트 로깅, 일일 공개 단가·환율 자동 동기화, Cloud Run 서비스까지 **한 번에 자동 구성**됩니다.

---

## Step 1. 원클릭 설치 실행

아래 실행 버튼을 클릭한 뒤, 터미널 안내에 따라 대표 프로젝트 번호(또는 Enter)를 입력하면 배포가 시작됩니다. 추가 연동할 프로젝트 번호를 함께 입력하면 **하나의 통합 대시보드**에서 여러 프로젝트를 동시에 관제할 수 있습니다.

```bash
./scripts/install.sh
```

> **팁 — 명령어 한 줄로 바로 실행하기**
> - **단일 프로젝트 배포**:
>   ```bash
>   ./scripts/install.sh --project my-project-id
>   ```
> - **여러 프로젝트를 하나의 대시보드로 통합 관제 (권장)**:
>   ```bash
>   ./scripts/install.sh --project hub-project-id --link-projects spoke-project-1,spoke-project-2
>   ```
> - **프로젝트마다 별도 대시보드 개별 배포**:
>   ```bash
>   ./scripts/install.sh --projects my-project-1,my-project-2
>   ```

---

## Step 2. 접속 확인

배포가 완료되면 터미널 하단에 출력된 `https://ai-control-plane-...run.app/login` 주소를 클릭해 바로 로그인합니다.

<walkthrough-conclusion-trophy></walkthrough-conclusion-trophy>
