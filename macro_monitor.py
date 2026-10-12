#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
프로그램명 : macro_monitor.py
설명 : STM32MP135F-DK 480x272 LCD용 금융/기상 매크로 대시보드 (PyGObject GTK+3)
"""

import sys
import os
import subprocess


# -------------------------------------------------------------------
# 운영체제 구분 (Windows vs Linux)
# -------------------------------------------------------------------
IS_WINDOWS = sys.platform.startswitch('win')

# -------------------------------------------------------------------
# Weston / Wayland 디스플레이 환경 변수 자동 설정
# -------------------------------------------------------------------
if not IS_WINDOWS:
    try:
        # weston 계정의 UID 구하기
        weston_uid = subprocess.check_output(["id", "-u", "weston"]).decode('utf-8').strip()
        os.environ["XDG_RUNTIME_DIR"] = f"/run/user/{weston_uid}"
    except Exception:
        os.environ["XDG_RUNTIME_DIR"] = "/run/user/1000"

os.environ["GDK_BACKEND"] = "wayland"
os.environ["WAYLAND_DISPLAY"] = "wayland-0"

import json
import time
import datetime
import configparser
import urllib.request
import urllib.error
import threading
import signal
import cairo
import ssl

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GLib, Pango



# -------------------------------------------------------------------
# CONFIG & CONSTANTS
# -------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.ini")

# 기본 색상 정의 (Hex)
COLOR_RED = "#D32F2F"      # 상승 (한국식)
COLOR_BLUE = "#1976D2"     # 하락 (한국식)
COLOR_GREEN = "#146614"    # 수급 배경
COLOR_GRAY = "#616161"     # 에러 / 데이터 없음
COLOR_PURPLE = "#4A148C"   # 금리 배경
COLOR_DAY_BG = "#C3C3C3"   # 일자 회색 계열
COLOR_CLOSE_TEXT = "#AFAFAF" # 장 마감 시 글씨 색


# -------------------------------------------------------------------
# HELPER FUNCTIONS
# -------------------------------------------------------------------
def fetch_json_with_retry(url, retries=3, delay=1.0):
    """3회 재시도 로직이 적용된 HTTP GET JSON 수집 함수"""
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    data = resp.read().decode('utf-8')
                    return json.loads(data)
        except Exception as e:
            time.sleep(delay)
    return None


# -------------------------------------------------------------------
# get weather icon path
# get weather korean
#
# Code          Weather status                                      Recommanded text display
# 0	            Clear sky	                                        맑음
# 1	            Mainly clear	                                    대체로 맑음
# 2	            Partly cloudy	                                    구름 조금
# 3	            Overcast	                                        흐림
# 45, 48	    Fog and depositing rime fog	                        안개
# 51, 53, 55	Drizzle: Light, moderate, and dense intensity	    이슬비
# 56, 57	    Freezing Drizzle: Light and dense intensity	        진눈깨비
# 61, 63, 65	Rain: Slight, moderate and heavy intensity	        비
# 66, 67	    Freezing Rain: Light and heavy	                    얼어붙는 비
# 71, 73, 75	Snow fall: Slight, moderate, and heavy intensity	눈
# 77	        Snow grains	                                        진눈깨비
# 80, 81, 82	Rain showers: Slight, moderate, and violent	        소나기
# 85, 86	    Snow showers: Slight and heavy	                    소나기 눈
# 95	        Thunderstorm: Slight or moderate	                뇌우
# 96, 99	    Thunderstorm with slight and heavy hail	            뇌우/우박
# -------------------------------------------------------------------

# WMO 날씨 코드 -> 한글 매핑 딕셔너리
WMO_WEATHER_KOR = {
    0: "맑음",
    1: "대체로 맑음",
    2: "구름 조금",
    3: "흐림",
    45: "안개", 48: "짙은 안개",
    51: "약한 이슬비", 53: "이슬비", 55: "강한 이슬비",
    56: "약한 얼음비", 57: "강한 얼음비",
    61: "약한 비", 63: "비", 65: "강한 비",
    66: "약한 진눈깨비", 67: "강한 진눈깨비",
    71: "약한 눈", 73: "눈", 75: "강한 눈",
    77: "진눈깨비",
    80: "약한 소나기", 81: "소나기", 82: "강한 소나기",
    85: "소나기 눈", 86: "강한 소나기 눈",
    95: "뇌우", 96: "뇌우/우박", 99: "강한 뇌우/우박"
}

def get_weather_korean(code):
    """WMO 날씨 코드를 한글 텍스트로 변환 (없는 코드는 '구름 조금' 기본 처리)"""
    return WMO_WEATHER_KOR.get(code, "구름 조금")


# -------------------------------------------------------------------
# MAIN DASHBOARD WINDOW
# -------------------------------------------------------------------
class MacroMonitor(Gtk.Window):
    def __init__(self):
        super().__init__(title="Macro Monitor")
        self.set_decorated(False)
        self.maximize()

        self.config = configparser.ConfigParser()
        self.load_config()

        # 백라이트 제어용 변수 (1~10 반복)
        self.brightness_val = 1
        self.brightness_step = 1
        self.find_backlight_path()

        # CSS 스타일 프로바이더 등록
        self.css_provider = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            self.css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        # 타일 데이터 스토어 & UI 객체 생성
        self.tiles = {}
        self.build_ui()

        # 타이머 등록 (60초 마다 시각 & 디밍 조절, 주기별 데이터 수집)
        GLib.timeout_add_seconds(60, self.on_minute_timer)

        # 초기 수집 실행
        threading.Thread(target=self.refresh_all_data, daemon=True).start()

        # pkill -SIGUSR1 명령용 시그널 등록
        signal.signal(signal.SIGUSR1, self.handle_capture_signal)

        # 터미널 키보드 Ctrl + \ (SIGQUIT) 가로채기 등록
        signal.signal(signal.SIGQUIT, self.handle_capture_signal)

    # ---------------------------------------------------------------
    # 백라이트 sysfs 경로 검색 및 제어(STM32MP135F-DK 전용) 
    # ---------------------------------------------------------------

    def find_backlight_path(self):
        if IS_WINDOWS:
            return

        self.backlight_path = None
        base_dir = "/sys/class/backlight"
        if os.path.exists(base_dir):
            entries = os.listdir(base_dir)
            if entries:
                self.backlight_path = os.path.join(base_dir, entries[0], "brightness")


    def update_backlight(self):
        if IS_WINDOWS:
            return

        """1초마다 1~10 사이를 왕복하며 백라이트 제어"""
        if self.backlight_path and os.path.exists(self.backlight_path):
            try:
                # sysfs brightness에 값 쓰기
                with open(self.backlight_path, "w") as f:
                    f.write(str(self.brightness_val))
            except Exception:
                pass

        # 1 -> 10 -> 1 반복 증감 로직
        self.brightness_val += self.brightness_step
        if self.brightness_val >= 10:
            self.brightness_val = 10
            self.brightness_step = -1
        elif self.brightness_val <= 1:
            self.brightness_val = 1
            self.brightness_step = 1


    # ---------------------------------------------------------------
    # CONFIG LOAD
    # ---------------------------------------------------------------
    def load_config(self):
        if os.path.exists(CONFIG_FILE):
            try:
                self.config.read(CONFIG_FILE, encoding='utf-8')
            except Exception:
                pass


    # ---------------------------------------------------------------
    # SCREEN CAPTURE
    # ---------------------------------------------------------------
    def handle_capture_signal(self, signum, frame):
        """시그널 수신 시 메인 GTK 스레드에서 화면 캡처 실행"""
        GLib.idle_add(self.save_screenshot)


    def save_screenshot(self):
        """현재 화면을 /usr/local/share/screenshot.png 로 저장"""
        try:
            width = 480
            height = 272

            surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
            cr = cairo.Context(surface)

            self.draw(cr)

            filepath = "/usr/local/share/screenshot.png"
            surface.write_to_png(filepath)
            print(f"\n[Cairo Direct Render Success] Saved to {filepath}", flush=True)
        except Exception as e:
            print(f"\n[Screenshot Exception] {e}", flush=True)
        return False


    # ---------------------------------------------------------------
    # UI BUILDER
    # ---------------------------------------------------------------
    def build_ui(self):
        TOTAL_WIDTH = 480               # LCD 전체 가로 해상도
        TOTAL_HEIGHT = 272              # LCD 전체 세로 해상도
        BORDER_WIDTH = 1                # CSS border 두께 (1px)
        BORDER_TOTAL = BORDER_WIDTH * 2 # 타일당 상하 테두리 합 (2px)

        TITLE_MARGIN_TOP = 2            # 제목 라벨 상단 여백
        TITLE_MARGIN_START = 4          # 제목 라벨 왼쪽 여백
        CONTENT_MARGIN_TOP = 0          # 수치 라벨 상단 여백

        # 왼쪽 타일(US 국채 수익률, 환율)
        US_T_H = 20                     # US TITLE HEIGHT
        US_H = 37                       # US HEIGHT
        EX_H = 47                       # EXCHANGE RATE HEIGHT (47)

        L_W = 120                       # 좌측 패널 너비
        l_y0 = 0                        # US 국채 수익률 TITLE
        l_y1 = l_y0 + US_T_H            # 2Y
        l_y2 = l_y1 + US_H              # 10Y
        l_y3 = l_y2 + US_H              # 30Y
        l_y4 = l_y3 + US_H              # USD-KRW
        l_y5 = l_y4 + EX_H              # USD-JPY
        l_y6 = l_y5 + EX_H              # JPY-KRW
        L_H_LAST = TOTAL_HEIGHT - l_y6

        # 오른쪽 상단 5개 타일(kospi, sp500, us_title, us2y, us10y)의 지정 높이
        R_S = 340                       # 우측 X 시작 지점
        R_W = TOTAL_WIDTH - R_S         # 우측 패널 너비
        R_H = 70                        # 우측 패널 높이
        R_INV_H = 60                    # 우측 수급 패널 높이

        r_y0 = 0                        # kospi
        r_y1 = r_y0 + R_H               # investors
        r_y2 = r_y1 + R_INV_H           # S&P500
        r_y3 = r_y2 + R_H               # 비트코인
        R_H_LAST = TOTAL_HEIGHT - r_y3

        # 가운데 타일
        M_S = L_W                       # 중앙 X 시작 지점
        M_W = TOTAL_WIDTH - L_W - R_W   # 중앙 패널 너비

        M_RAT_S = M_S + M_W - 80        # 중앙 금리 패널 X 시작 지점
        M_RAT_H = 72                    # 중앙 금리 패널 높이
        M_RAT_Y = 72                    # 중앙 금리 패널 Y 시작 지점
        M_RAT_W = 80                    # 중앙 금리 패널 너비

        M_DAY_S = M_S + 80              # 중앙 날짜 X 시작 지점
        M_DAY_Y = 0                     # 중앙 날짜 패널 Y 시작 지점
        M_DAY_W = M_W - M_RAT_W         # 중앙 날짜 패널 너비
        M_DAY_H = 72                    # 중앙 날짜 패널 높이

        M_TIME_S = M_S                  # 중앙 시간 X 시작 지점
        M_TIME_Y = 72                   # 중앙 시간 Y 시작 지점
        M_TIME_W = M_W - M_RAT_W        # 중앙 시간 패널 너비
        M_TIME_H = 72                   # 중앙 시간 패널 높이

        M_MON_S = M_S                   # 중앙 날짜(월) Y 시작 지점
        M_MON_Y = M_DAY_H + M_TIME_H    # 중앙 날짜(월) Y 시작 지점
        M_MON_W = M_W                   # 중앙 날짜(월) 패널 너비
        M_MON_H = TOTAL_HEIGHT - M_DAY_H - M_TIME_H

        # 타일 배치 정의 (key: [x, y, w, h])
        layout = {
            # 왼쪽 (미국국채수익률/금리)
            "us_title":   [0, l_y0, L_W, US_T_H],
            "us2y":       [0, l_y1, L_W, US_H],
            "us10y":      [0, l_y2, L_W, US_H],
            "us30y":      [0, l_y3, L_W, US_H],
            "usdkrw":     [0, l_y4, L_W, EX_H],
            "usdjpy":     [0, l_y5, L_W, EX_H],
            "100jpy":     [0, l_y6, L_W, L_H_LAST],

            # 중앙
            "weather":   [M_S, 0, 80, 72],
            "day":       [M_DAY_S, M_DAY_Y, M_DAY_W, M_DAY_H],

            "hour":      [M_TIME_S, M_TIME_Y, M_TIME_W, M_TIME_H],

            "rate":      [M_RAT_S, M_RAT_Y, M_RAT_W, M_RAT_H],

            "month":     [M_MON_S, M_MON_Y, M_MON_W, M_MON_H],   # deprecated

            # 오른쪽
            "kospi":     [R_S, r_y0, R_W, R_H],
            "investors": [R_S, r_y1, R_W, R_INV_H],
            "sp500":     [R_S, r_y2, R_W, R_H],
            "btc":       [R_S, r_y3, R_W, R_H_LAST],
        }

        # 왼쪽 전용 좌표 기반 고정 레이아웃 생성
        self.fixed_layout = Gtk.Fixed()
        self.fixed_layout.set_size_request(TOTAL_WIDTH, TOTAL_HEIGHT)
        self.add(self.fixed_layout)

        for key, pos in layout.items():
            box = Gtk.EventBox()
            box.set_size_request(pos[2], pos[3])
            box.set_name(key)

            vbox = Gtk.VBox(homogeneous=False, spacing=0)
            vbox.set_valign(Gtk.Align.START)                # START: 상단, CENTER: 중앙, FILL: ??

            title_lbl = Gtk.Label()
            title_lbl.set_xalign(0.0)                       # 0.0: 왼쪽 밀착, 0.5: 중앙, 1.0: 오른쪽
            title_lbl.set_justify(Gtk.Justification.LEFT)   # LEFT: 왼쪽, CENTER: 중앙
            title_lbl.set_margin_start(TITLE_MARGIN_START)  # 왼쪽 여백 4px
            title_lbl.set_margin_top(TITLE_MARGIN_TOP)      # 상단 여백 2px

            content_lbl = Gtk.Label()
            content_lbl.set_justify(Gtk.Justification.CENTER)
            content_lbl.set_halign(Gtk.Align.CENTER)
            content_lbl.set_margin_top(CONTENT_MARGIN_TOP)
            content_lbl.set_vexpand(False)

            if key == "us_title":
                title_lbl.set_markup("<span font='8' color='#FFFFFF'><b>US 국채 수익률</b></span>")
                vbox.pack_start(title_lbl, False, False, 0)
            elif key in ["us2y", "us10y", "us30y", "investors", "day", "hour"]:
                vbox.pack_start(content_lbl, True, True, 0)
            else:
                vbox.pack_start(title_lbl, False, False, 0)     # 상단 여백 0px
                vbox.pack_start(content_lbl, True, True, 0)     # 수치는 남은 공간 중앙 채움

            box.add(vbox)

            self.fixed_layout.put(box, pos[0], pos[1])

            self.tiles[key] = {
                "box": box,
                "title": title_lbl,
                "content": content_lbl,
                "bg": COLOR_GRAY
            }

        self.update_tile_ui("us_title", "<span font='8' color='#FFFFFF'><b>US 국채 수익률</b></span>", "", COLOR_GREEN)

        self.apply_css()


    def apply_css(self):
        """GTK Dynamic CSS 배경색 적용"""
        css = "* { font-family: 'Malgun Gothic', '맑은 고딕', sans-serif; }\n"
        for key, tile in self.tiles.items():
            css += f"#{key} {{ background-color: {tile['bg']}; border: 1px solid #000000; }}\n"
        self.css_provider.load_from_data(css.encode('utf-8'))


    def update_tile_ui(self, key, title_markup, content_markup, bg_color):
        def _update():
            if key in self.tiles:
                self.tiles[key]["title"].set_markup(title_markup)

                self.tiles[key]["content"].set_markup(content_markup)

                if self.tiles[key]["bg"] != bg_color:
                    self.tiles[key]["bg"] = bg_color
                    self.apply_css()
            return False

        GLib.idle_add(_update)


    # ---------------------------------------------------------------
    # TIMERS & REFRESH LOGIC
    # ---------------------------------------------------------------
    def on_second_timer(self):

        # 1. LCD 디밍 처리
        self.update_backlight()

        # 2. 시각/일자 갱신 (1초)
        now = datetime.datetime.now()
        
        # 시간 타일 (시간대별 배경색 조절)
        hour = now.hour
        if 6 <= hour < 12:
            time_bg = "#F57F17"   # 아침/낮
        elif 12 <= hour < 18:
            time_bg = "#E65100"   # 오후
        elif 18 <= hour < 21:
            time_bg = "#BF360C"   # 노을/저녁
        else:
            time_bg = "#1A237E"   # 밤/새벽

        self.update_tile_ui("hour", "",
                            f"<span font='36' color='#FFFF00'><b>{hour:02d}</b></span>"
                            f"<span font='10' color='#FFFF00'>시</span>"
                            f"<span font='36' color='#FFFF00'><b>{now.minute:02d}</b></span>"
                            f"<span font='10' color='#FFFF00'>분</span>",
                            time_bg)

        # 날짜 타일
        weekday = now.weekday()

        # 0~6 인덱스를 한글 요일 문자로 바로 매핑(월~일)
        weekdays_kor = ["월", "화", "수", "목", "금", "토", "일"]
        kor_day = weekdays_kor[weekday]

        day_color = "#000000"
        if weekday == 5:
            day_color = "#1976D2" # 토요일
        elif weekday == 6:
            day_color = "#D32F2F" # 일요일

        # deprecated
        self.update_tile_ui("month", "",
                            "", COLOR_DAY_BG)
        self.update_tile_ui("day", "",
                            f"<span font='12' color='#000000'>{now.month}월</span><span font='40' color='{day_color}'><b>{now.day}</b></span><span font='10' color='{day_color}'>{kor_day}요일</span>", COLOR_DAY_BG)

        return True


    def on_minute_timer(self):
        self.on_second_timer()
        now = datetime.datetime.now()
        m = now.minute
        h = now.hour

        # 환율: 15분, 45분
        if m in [15, 45]:
            threading.Thread(target=self.fetch_exchange_rates, daemon=True).start()

        # 비트코인: 5, 15, 25, 35, 45, 55분
        if m in [5, 15, 25, 35, 45, 55]:
            threading.Thread(target=self.fetch_bitcoin, daemon=True).start()

        # 코스피 / S&P500 / 수급: 10, 25, 40, 55분
        if m in [10, 25, 40, 55]:
            threading.Thread(target=self.refresh_stocks_investors, daemon=True).start()

        # 날씨: 매시 5분
        if m == 5:
            threading.Thread(target=self.fetch_weather, daemon=True).start()

        # 국채수익률: 매시 35분
        if m == 35:
            threading.Thread(target=self.refresh_us_bonds, daemon=True).start()

        # 금리: 오전 5시 0분
        if h == 5 and m == 0:
            self.load_config()
            self.update_interest_rates()

        return True


    def refresh_stocks_investors(self):
        self.fetch_stock_indices()
        self.fetch_kospi_investors()


    def refresh_us_bonds(self):
        self.fetch_us_bonds()


    def refresh_all_data(self):
        """초기 실행 시 전체 데이터 병렬 수집"""
        self.fetch_exchange_rates()
        self.fetch_bitcoin()
        self.refresh_stocks_investors()
        self.fetch_weather()
        self.refresh_us_bonds()
        self.update_interest_rates()


    # ---------------------------------------------------------------
    # DATA FETCHING THREADS
    # ---------------------------------------------------------------
    def fetch_exchange_rates(self):
        # Yahoo Finance API (USDKRW=X, USDJPY=X)
        tickers = ["USDKRW=X", "USDJPY=X"]
        for t in tickers:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{t}?interval=1d"
            res = fetch_json_with_retry(url)
            if res and "chart" in res and res["chart"]["result"]:
                meta = res["chart"]["result"][0]["meta"]
                price = meta.get("regularMarketPrice", 0)
                prev = meta.get("chartPreviousClose", price)
                bg = COLOR_RED if price >= prev else COLOR_BLUE

                if t == "USDKRW=X":
                    self.update_tile_ui("usdkrw", "<span font='8' color='#FFFFFF'>1달러</span>",
                                        f"<span font='18' color='#FFFFFF'><b>{int(price):,}</b></span> <span font='10' color='#FFFFFF'>KRW</span>", bg)
                elif t == "USDJPY=X":
                    self.update_tile_ui("usdjpy", "<span font='8' color='#FFFFFF'>1달러</span>",
                                        f"<span font='18' color='#FFFFFF'><b>{int(price)}</b></span> <span font='10' color='#FFFFFF'>JPY</span>", bg)
            else:
                target = "usdkrw" if t == "USDKRW=X" else "usdjpy"
                self.update_tile_ui(target, "<span font='8' color='#FFFFFF'>1달러</span>",
                                    "<span font='16' color='#FFFFFF'>Error</span>", COLOR_GRAY)

        # 원-엔(100엔) 계산
        # 100 JPY in KRW = (USDKRW / USDJPY) * 100
        try:
            url_krw = "https://query1.finance.yahoo.com/v8/finance/chart/USDKRW=X?interval=1d"
            url_jpy = "https://query1.finance.yahoo.com/v8/finance/chart/USDJPY=X?interval=1d"
            r_krw = fetch_json_with_retry(url_krw)
            r_jpy = fetch_json_with_retry(url_jpy)
            p_krw = r_krw["chart"]["result"][0]["meta"]["regularMarketPrice"]
            p_jpy = r_jpy["chart"]["result"][0]["meta"]["regularMarketPrice"]
            calc_100jpy = (p_krw / p_jpy) * 100
            prev_krw = r_krw["chart"]["result"][0]["meta"]["chartPreviousClose"]
            prev_jpy = r_jpy["chart"]["result"][0]["meta"]["chartPreviousClose"]
            prev_100jpy = (prev_krw / prev_jpy) * 100

            bg = COLOR_RED if calc_100jpy >= prev_100jpy else COLOR_BLUE
            self.update_tile_ui("100jpy", "<span font='8' color='#FFFFFF'>100엔</span>",
                                f"<span font='18' color='#FFFFFF'><b>{int(calc_100jpy):,}</b></span> <span font='10' color='#FFFFFF'>KRW</span>", bg)
        except Exception:
            self.update_tile_ui("100jpy", "<span font='8' color='#FFFFFF'>100엔</span>",
                                "<span font='16' color='#FFFFFF'>Error</span>", COLOR_GRAY)


    def fetch_bitcoin(self):
        url = "https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT"
        res = fetch_json_with_retry(url)
        if res and "lastPrice" in res:
            price = float(res["lastPrice"])
            open_p = float(res["openPrice"])
            bg = COLOR_RED if price >= open_p else COLOR_BLUE
            self.update_tile_ui("btc", "<span font='8' color='#FFFFFF'>비트코인</span>",
                                f"<span font='18' color='#FFFFFF'><b>{int(price):,}</b></span> <span font='10' color='#FFFFFF'>USD</span>", bg)
        else:
            self.update_tile_ui("btc", "<span font='8' color='#FFFFFF'>비트코인</span>",
                                "<span font='14' color='#FFFFFF'>Error</span>", COLOR_GRAY)


    def fetch_stock_indices(self):
        indices = [("^KS11", "kospi", "코스피"), ("^GSPC", "sp500", "S&amp;P500")]
        now = datetime.datetime.now()
        now_ts = time.time()  # 현재 Unix Timestamp

        for ticker, tile_key, name in indices:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d"
            res = fetch_json_with_retry(url)
            if res and "chart" in res and res["chart"]["result"]:
                meta = res["chart"]["result"][0]["meta"]
                price = meta.get("regularMarketPrice", 0)
                prev = meta.get("chartPreviousClose", price)
                
                i_part = int(price)
                f_part = f"{price - i_part:.2f}"[1:]

                bg = COLOR_RED if price >= prev else COLOR_BLUE

                # ---------------------------------------------------------
                # [장 마감 판정 로직]
                # 1. 야후 currentTradingPeriod -> regular 범위 확인
                # ---------------------------------------------------------
                is_closed = True
                trading_period = meta.get("currentTradingPeriod", {}).get("regular", {})
                start_ts = trading_period.get("start")
                end_ts = trading_period.get("end")

                if start_ts and end_ts:
                    # 현재 timestamp가 regular 개장~마감 시간 안인지 확인
                    is_closed = not (start_ts <= now_ts <= end_ts)
                else:
                    # Fallback: 야후 응답에 타임스탬프가 없을 경우 한국 시간 기준 단순 판정
                    if tile_key == "kospi":
                        # 평일(월~금) 09:00 ~ 15:30
                        is_weekday = now.weekday() < 5
                        is_market_time = datetime.time(9, 0) <= now.time() <= datetime.time(15, 30)
                        is_closed = not (is_weekday and is_market_time)
                    elif tile_key == "sp500":
                        # 미국 S&P500 (야간 시간대 등)
                        is_closed = not (now.hour >= 22 or now.hour < 6)

                text_color = COLOR_CLOSE_TEXT if is_closed else "#FFFFFF"
                
                title_text = f"<span font='8' color='#FFFFFF'>{name}</span>"
                if is_closed:
                    title_text += " <span font='8' color='#FFFFFF'>   [마감]</span>"

                markup = f"<span font='24' color='{text_color}'><b>{i_part:,}</b></span><span font='14' color='{text_color}'>{f_part}</span>"
                
                self.update_tile_ui(tile_key, title_text, markup, bg)
            else:
                self.update_tile_ui(tile_key, f"<span font='8' color='#FFFFFF'>{name}</span>",
                                    "<span font='14' color='#FFFFFF'>Error</span>", COLOR_GRAY)


    def fetch_kospi_investors(self):
        """네이버 증권 API를 활용한 코스피 투자자별 매매동향(외국인, 기관, 개인) 수집"""
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Referer': 'https://m.stock.naver.com/'
        }

        url = "https://m.stock.naver.com/api/index/KOSPI/trend"
        
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode('utf-8'))
                    
                    # 문자열로 들어올 경우 콤마 제거 후 정수 변환 헬퍼
                    def parse_int(val):
                        if isinstance(val, (int, float)):
                            return int(val)
                        if isinstance(val, str):
                            clean = val.replace(',', '').replace('억', '').strip()
                            return int(clean) if clean and clean != '-' else 0

                    # /trend API는 리스트 형태 [ { "bizdate": ..., "personalValue": ... }, ... ] 이거나 단일 객체
                    target = data[0] if isinstance(data, list) and len(data) > 0 else data

                    # 수급 데이터 추출 (외국인, 기관, 개인. 단위: 억 원)
                    # foreignValue (외국인), institutionalValue (기관), personalValue (개인)
                    foreign = parse_int(target.get("foreignValue") or target.get("foreignPureBuyQuant") or 0)
                    organ = parse_int(target.get("institutionalValue") or target.get("organPureBuyQuant") or 0)
                    individual = parse_int(target.get("personalValue") or target.get("individualPureBuyQuant") or 0)

                    # 부호 및 콤마 포맷팅 헬퍼
                    # 수치 우측 정렬
                    def fmt(val):
                        sign = "+" if val > 0 else ("-" if val < 0 else " ")
                        abs_str = f"{abs(val):,}"
                        num_str = f"{sign}{abs_str}억"
                        # 8자리에 맞춰 오른쪽 정렬 (자릿수가 모자라면 앞쪽에 공백 채움)
                        return num_str.rjust(8)
    
                    # 마크업 구성 (외인: +1,200억 / 기관: -500억 / 개인: -700억)
                    # Monospace(고정폭) 폰트를 지정하여 숫자와 공백의 가로 폭을 일치시킴
                    markup = (
                        f"<span font='10' face='Monospace' color='#FFFFFF'>"
                        f"외인 <span color='{'#FF5252' if foreign >= 0 else '#849AFF'}'><b>{fmt(foreign)}\n</b></span>"
                        f"기관 <span color='{'#FF5252' if organ >= 0 else '#849AFF'}'><b>{fmt(organ)}\n</b></span>"
                        f"개인 <span color='{'#FF5252' if individual >= 0 else '#849AFF'}'><b>{fmt(individual)}\n</b></span>"
                        f"</span>"
                    )
                    self.update_tile_ui("investors", "", markup, COLOR_GREEN)

        except Exception as e:
            print(f"[Investor Fetch Error] {e}", flush=True)
            self.update_tile_ui("investors", "", "<span font='12' color='#FFFFFF'>Error</span>", COLOR_GRAY)


    def fetch_weather(self):
        lat = self.config.get("LOCATION", "latitude", fallback="37.4852")
        lon = self.config.get("LOCATION", "longitude", fallback="126.8386")
        url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current_weather=true"
        res = fetch_json_with_retry(url)

        if res and "current_weather" in res:
            cw = res["current_weather"]
            temp = float(cw.get("temperature", 0))
            code = int(cw.get("weathercode", 0))

            if temp <= 0:
                bg = "#0288D1" # 하늘색/파란색
            elif temp < 20:
                bg = "#F57C50" # 주황색(처음값 #F57C00)
            else:
                bg = "#D32F2F" # 빨간색

            # 날씨 아이콘을 구하는게 시간이 오래 걸릴것 같아 글씨로 표현하고자 함
            weather_str = get_weather_korean(code)
            self.update_tile_ui("weather", f"<span font='8' color='#FFFFFF'><b>{weather_str}</b></span>",
                                f"<span font='18' color='#FFFFFF'><b>{int(temp)}°</b></span>", bg)
        else:
            self.update_tile_ui("weather", "<span font='7' color='#FFFFFF'>온도</span>",
                                "<span font='10' color='#FFFFFF'>ERR</span>", COLOR_GRAY)


    def fetch_us_bonds(self):
        # 2Y, 10Y, 30Y
        bonds = [("2YY=F","us2y","2Y"),("^TNX", "us10y", "10Y"), ("^TYX", "us30y", "30Y")]
        for ticker, key, label in bonds:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d"
            res = fetch_json_with_retry(url)
            if res and "chart" in res and res["chart"]["result"]:
                meta = res["chart"]["result"][0]["meta"]
                price = meta.get("regularMarketPrice", 0)
                prev = meta.get("chartPreviousClose", price)
                bg = COLOR_RED if price >= prev else COLOR_BLUE

                # 상단 제목은 빈칸으로 두고, 수치와 만기(2Y/10Y/30Y)만 큼직하게 표시
                markup = f"<span font='20' color='#FFFFFF'><b>{price:.3f}</b></span> <span font='11' color='#FFFFFF'>{label}</span>"
                self.update_tile_ui(key, "", markup, bg)
            else:
                self.update_tile_ui(key, "", "<span font='12' color='#FFFFFF'>Error</span>", COLOR_GRAY)


    def fetch_us_bonds_2y(self):
        """FRED 2년물 국채 금리 수집 (JSON 엔드포인트)"""
        url = "https://fred.stlouisfed.org/graph/fredgraph.json?id=DGS2"
        res = fetch_json_with_retry(url)

        if res and "obs" in res:
            # 유효한 수치 데이터만 필터링 (결측치 '.' 제외)
            valid_obs = [o for o in res["obs"] if o.get("value") != "."]
            if len(valid_obs) >= 2:
                price = float(valid_obs[-1]["value"])
                prev = float(valid_obs[-2]["value"])

                bg = COLOR_RED if price >= prev else COLOR_BLUE
                markup = f"<span font='20' color='#FFFFFF'><b>{price:.3f}</b></span> <span font='11' color='#FFFFFF'>2Y</span>"
                self.update_tile_ui("us2y", "", markup, bg)
            else:
                self.update_tile_ui("us2y", "", "<span font='12' color='#FFFFFF'>Error</span>", COLOR_GRAY)
        else:
            self.update_tile_ui("us2y", "", "<span font='12' color='#FFFFFF'>Error</span>", COLOR_GRAY)


    def fetch_us_bonds_2y_old(self):
        """FRED(미국 연준) 공식 데이터를 활용한 국채 현물 금리 수집"""
        # 1. urllib 전용 SSL 컨텍스트 생성 (지연 방지)
        ssl_ctx = ssl._create_unverified_context()
        
        # 2. FRED 서버가 딜레이 없이 응답하도록 만드는 완전한 헤더 조합
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
            'Connection': 'close'
        }

        try:
            url_2y = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS2"
            req = urllib.request.Request(url_2y, headers=headers)

            with urllib.request.urlopen(req, timeout=10, context=ssl_ctx) as resp:
                if resp.status == 200:
                    lines = resp.read().decode('utf-8').strip().split('\n')
                    # 최신 날짜의 금리 데이터 추출 (맨 마지막 줄)
                    valid_lines = [l for l in lines if not l.endswith('.') and ',' in l]
                    if len(valid_lines) >= 2:
                        last_date, last_price = valid_lines[-1].split(',')
                        prev_date, prev_price = valid_lines[-2].split(',')

                        price = float(last_price)
                        prev = float(prev_price)

                        bg = COLOR_RED if price >= prev else COLOR_BLUE
                        markup = f"<span font='20' color='#FFFFFF'><b>{price:.3f}</b></span> <span font='11' color='#FFFFFF'>2Y</span>"
                        self.update_tile_ui("us2y", "", markup, bg)
        except Exception as e:
            print(f"[US Bond Fetch Error us2y] {e}", flush=True)
            self.update_tile_ui("us2y", "", "<span font='12' color='#FFFFFF'>Error</span>", COLOR_GRAY)


    def update_interest_rates(self):
        try:
            kr = self.config.get("INTEREST_RATE", "kr", fallback="--.--%")
            us = self.config.get("INTEREST_RATE", "us", fallback="--.--%")
            jp = self.config.get("INTEREST_RATE", "jp", fallback="--.--%")
            markup = f"<span font='10' color='#FFFFFF'><b>KR {kr}%\nUS {us}%\nJP {jp}%</b></span>"
            self.update_tile_ui("rate", "<span font='8' color='#FFFFFF'>금리</span>", markup, COLOR_PURPLE)
        except Exception:
            markup = "<span font='10' color='#FFFFFF'><b>KR --.--%\nUS --.--%\nJP --.--%</b></span>"
            self.update_tile_ui("rate", "<span font='8' color='#FFFFFF'>금리</span>", markup, COLOR_PURPLE)


# -------------------------------------------------------------------
# MAIN ENTRY
# -------------------------------------------------------------------
if __name__ == "__main__":
    app = MacroMonitor()
    app.connect("destroy", Gtk.main_quit)
    app.show_all()
    Gtk.main()
