# 로컬 LLM 서버 (Ollama) — 모든 가속 경로를 한 이미지로 지원한다.
#   - NVIDIA          : CUDA 백엔드 (Ollama 배포본에 포함, 컨테이너에 GPU 전달 시 자동 사용)
#   - Intel/AMD (Linux): Vulkan (Mesa ANV/RADV 드라이버, /dev/dri 전달)
#   - Intel/AMD (Windows Docker Desktop = WSL2): Vulkan on D3D12 (Mesa Dozen 드라이버, /dev/dxg 전달)
#   - 그 외            : CPU
# 공식 ollama/ollama 이미지는 Ubuntu 기반이라 Dozen(dzn) 드라이버가 없어 WSL2에서 Intel GPU를 못 쓴다.
# 그래서 Dozen을 배포하는 Arch Linux 위에 공식 Ollama 배포본을 올린다.
# 어떤 장치를 컨테이너에 넘길지는 scripts/setup_local_llm.py가 호스트를 감지해 docker-compose.override.yml로 정한다.
#
# 버전 고정: 기본 이미지 digest / Arch 저장소 스냅샷 날짜 / Ollama 버전 + SHA-256

FROM archlinux:latest@sha256:f3691b4dde62ba4c4b6f0ae2c1fbf28e8c0c8c4b9a35c7e06dc1f70e21aa29f6

# Arch는 롤링 배포라 날짜 스냅샷(Arch Linux Archive)으로 패키지 버전을 고정한다
ARG ARCH_SNAPSHOT=2026/09/27
RUN echo "Server = https://archive.archlinux.org/repos/${ARCH_SNAPSHOT}/\$repo/os/\$arch" > /etc/pacman.d/mirrorlist \
 && pacman -Syyuu --noconfirm \
 && pacman -S --noconfirm --needed vulkan-icd-loader vulkan-dzn vulkan-intel vulkan-radeon zstd curl ca-certificates \
 && pacman -Scc --noconfirm

ARG OLLAMA_VERSION=0.34.4
ARG OLLAMA_SHA256=c238986e61d40c0cc5f4a9b9e40b9eea104350b77efa34741fc134e105cb9533
RUN curl -fsSL -o /tmp/ollama.tar.zst \
      "https://github.com/ollama/ollama/releases/download/v${OLLAMA_VERSION}/ollama-linux-amd64.tar.zst" \
 && echo "${OLLAMA_SHA256}  /tmp/ollama.tar.zst" | sha256sum -c - \
 && tar --zstd -xf /tmp/ollama.tar.zst -C /usr \
 && rm /tmp/ollama.tar.zst \
 && ollama --version || true

# WSL2의 D3D12 사용자 모드 드라이버(libd3d12, libdxcore)는 호스트의 /usr/lib/wsl에서 마운트된다
ENV LD_LIBRARY_PATH=/usr/lib/wsl/lib \
    OLLAMA_HOST=0.0.0.0:11434
EXPOSE 11434
ENTRYPOINT ["/usr/bin/ollama"]
CMD ["serve"]
