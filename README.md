# JLPT 일본어 단어장 — 클라우드 최종 버전

이 버전은 Railway + PostgreSQL 배포를 기준으로 준비되어 있습니다.

## 포함 기능

- 기초 일본어 단어(N5/N4), N3, N2, N1 복수 필터
- 수기 단어 입력
- 단어 수정 / 개별 삭제 / 선택 삭제
- 중복 단어 방지
- 하루 최대 100개 학습
- 오늘의 테스트 / 누적 복습
- 한자 먼저 노출 → 정답 확인 후 요미가나 + 뜻 표시
- 일본어 발음 1.0배속
- 오답 중복 없이 누적
- 오답 하루 최대 5회독
- 5회 중 3회 이상 정답 시 학습 완료
- 학습 완료 후 다시 틀리면 오답 노트로 복귀
- 실제 학습일 기준 연속 학습 기록
- YouTube 링크에서 자막 가져오기
- YouTube 자막 직접 붙여넣기 fallback
- JSON 백업 내보내기 / 불러오기
- PostgreSQL 서버 DB 저장
- DATABASE_URL이 없으면 로컬 SQLite fallback

## Railway 배포 순서

### 1. GitHub 저장소 만들기

1. GitHub에 로그인합니다.
2. 새 Repository를 만듭니다. 예: `jlpt-vocab`.
3. 이 ZIP의 압축을 풀고, **폴더 안의 파일들**을 저장소 루트에 업로드합니다.
   - `main.py`
   - `requirements.txt`
   - `Dockerfile`
   - `railway.json`
   - `index.html`
   - 기타 파일

중요: 이 버전은 `static` 폴더가 없습니다.
압축을 푼 뒤 보이는 파일을 전부 GitHub 저장소 루트에 업로드하면 됩니다.
GitHub 첫 화면에서 `main.py`, `index.html`, `Dockerfile` 등이 바로 보여야 합니다.

### 2. Railway에서 앱 배포

1. Railway에 로그인합니다.
2. New Project → Deploy from GitHub repo를 선택합니다.
3. 방금 만든 `jlpt-vocab` 저장소를 선택합니다.
4. 첫 배포를 시작합니다.

이 프로젝트에는 Dockerfile과 railway.json이 포함되어 있어 Railway가 이를 사용해 앱을 실행합니다.

### 3. PostgreSQL 추가

Railway 프로젝트 화면에서:

1. Create 또는 + New를 선택합니다.
2. Database → PostgreSQL을 추가합니다.
3. 앱 서비스의 Variables 탭을 엽니다.
4. 아래 변수를 추가합니다.

이름:
`DATABASE_URL`

값:
`${{Postgres.DATABASE_URL}}`

PostgreSQL 서비스 이름이 `Postgres`가 아니라면 해당 서비스 이름에 맞춰 선택하세요.
Railway 변수 입력창의 자동완성을 이용하는 것이 가장 안전합니다.

### 4. 앱 다시 배포

DATABASE_URL을 추가하면 앱을 Redeploy합니다.

정상 연결 확인:
`https://내주소/api/health`

예상 응답:
`{"ok":true,"database":"postgresql"}`

### 5. 공개 URL 만들기

앱 서비스 → Settings → Networking → Public Networking → Generate Domain

Railway가 `*.up.railway.app` 형태의 URL을 만들어줍니다.

이 주소를:
- Windows Chrome
- Mac Safari
- iPhone Safari
- Android Chrome

에서 동일하게 열 수 있습니다.

## 데이터 저장

학습 데이터는 PostgreSQL의 `app_state` 테이블에 JSONB 형태로 저장됩니다.
기기마다 별도 저장되는 localStorage 방식이 아니라 서버 DB를 사용하므로,
같은 URL을 열면 동일한 데이터를 이어서 사용합니다.

JSON 백업 기능도 유지되어 있으므로 정기적으로 백업 파일을 받아두는 것을 권장합니다.

## 보안 주의

현재 사용자의 요청에 따라 로그인/비밀번호가 없습니다.
따라서 공개 URL을 아는 사람은 이 단어장에 접근하고 데이터를 수정할 수 있습니다.

개인용으로만 사용할 경우 URL을 공유하지 마세요.
추후 원하면 PIN 또는 로그인 기능을 다시 추가할 수 있습니다.

## YouTube 가져오기 주의

YouTube 영상에 자막이 없거나, 자막 접근이 제한되거나,
YouTube 정책/응답 방식이 바뀌면 자동 불러오기가 실패할 수 있습니다.
이 경우 앱에 남겨둔 '자막 직접 붙여넣기' 기능을 사용할 수 있습니다.

## 로컬 테스트(선택)

DATABASE_URL 없이 실행하면 SQLite를 사용합니다.

```bash
python -m pip install -r requirements.txt
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

브라우저:
`http://127.0.0.1:8000`
