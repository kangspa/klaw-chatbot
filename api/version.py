"""
앱 버전 (한 곳에서만 관리한다 — 트레이·로그·백업 정보·exe 파일 속성에 쓰인다).

릴리스할 때는 이 값과 pyproject.toml의 version을 함께 올리고 v<버전> 태그를 push한다
(.github/workflows/release.yml이 태그와 두 값이 같은지 확인한 뒤 exe를 빌드해 Release에 올린다).
"""
APP_VERSION = "0.1.0"
