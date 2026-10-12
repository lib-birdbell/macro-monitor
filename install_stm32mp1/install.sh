#!/bin/bash
# install_stm32mp1/install.sh

echo "=== Macro Monitor STM32MP1 Installer ==="

# 1. 패키지 매니저(apt/dnf 등) 기반 의존성 설치
if command -v apt-get &> /dev/null; then
    sudo apt-get update
    sudo apt-get install -y python3-gi python3-gi-cairo gir1.2-gtk-3.0 curl
elif command -v dnf &> /dev/null; then
    sudo dnf install -y python3-gobject gtk3 curl
fi

# 2. 메인 스크립트 및 서비스 파일 설치 경로 복사
INSTALL_DIR="/opt/macromonitor"
sudo mkdir -p $INSTALL_DIR
sudo cp ../macro_monitor.py $INSTALL_DIR/

# 3. systemd 서비스 등록 (부팅 시 자동 시작)
cat << 'EOF' | sudo tee /etc/systemd/system/macromonitor.service
[Unit]
Description=Macro Monitor GTK Dashboard
After=weston-graphical-session.service
Wants=weston-graphical-session.service

[Service]
Type=simple
User=root
Environment=DISPLAY=:0
WorkingDirectory=/opt/macromonitor
Environment=XDG_RUNTIME_DIR=/run/user/1000
Environment=WAYLAND_DISPLAY=wayland-0
Environment=GDK_BACKEND=wayland
ExecStartPre=/bin/sleep 2
ExecStart=/usr/bin/python3 /opt/macromonitor/macro_monitor.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# 4. 서비스 활성화 및 시작
sudo systemctl daemon-reload
sudo systemctl enable macromonitor.service
sudo systemctl restart macromonitor.service

echo "=== Installation Completed & Service Started! ==="
