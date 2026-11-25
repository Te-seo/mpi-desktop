import os
import re
from datetime import datetime
from typing import Dict, List, Tuple
import openpyxl
from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
from openpyxl.utils import get_column_letter


_DATE_PARSE_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S",
    "%Y/%m/%d %H:%M:%S.%f",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
)


def _format_report_date(value) -> str:
    """Normalize any recognized date/timestamp string to ``YYYY-MM-DD``.

    Accepts ISO 8601 (with or without time/zone), ``YYYY/MM/DD`` dates,
    and a few common variants. Returns the original string unchanged if it can't
    be parsed, and "" for empty input.
    """
    if value is None:
        return ""
    s = str(value).strip()
    if not s:
        return ""
    try:
        return datetime.fromisoformat(s).strftime("%Y-%m-%d")
    except ValueError:
        pass
    for fmt in _DATE_PARSE_FORMATS:
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    m = re.match(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return s

def export_data_to_excel(save_path: str, results_summary: Dict, results_thermal: Dict):
    """
    엑셀 보고서를 생성하는 모듈화된 함수.
    """
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Perf&Heat"

    # --- 스타일 정의 ---
    styles = _get_excel_styles()

    # --- 헤더 정의 ---
    # Row 2 (Top), Row 3 (Bottom)
    headers_top = [
        "Device", "", "", "", "GOS ver", "Mode", "Game Ver", "Date", "Duration", 
        "FPS", "", "", "", "", 
        "Temperature(°C)", "", "", "Avg Power\n(mW)", "Link", "Comment"
    ]
    headers_bottom = [
        "Model", "SW ver", "OS ver", "HW ver", "", "", "", "", "", 
        "Max", "Median", "Avg", "Avg\n(전반 20분)", "Avg\n(후반 20분)", 
        "Start", "End", "MAX", "", "", ""
    ]
    
    top_merges = [
        (1, 4, "Device"), (10, 14, "FPS"), (15, 17, "Temperature(°C)")
    ]

    # --- 데이터 그룹화 (Game 기준) ---
    games = _group_by_game(results_summary)

    curr_row = 1
    for game_name, items in games.items():
        # 1. Game Title Row (Dark Blue)
        _write_game_title(ws, curr_row, game_name, len(headers_top), styles)
        curr_row += 1

        # 2. Header Rows
        _write_headers(ws, curr_row, headers_top, headers_bottom, top_merges, styles)
        curr_row += 2
        
        # 3. Data Rows
        # 모델별 그룹화 (병합 처리 위해)
        model_groups = _group_by_model(items)

        for model, subset in model_groups.items():
            model_start_row = curr_row
            for f, s in subset:
                _write_data_row(ws, curr_row, f, s, results_thermal, styles)
                curr_row += 1
            
            # 모델 셀 병합
            if len(subset) > 1:
                ws.merge_cells(start_row=model_start_row, start_column=1, end_row=curr_row-1, end_column=1)

        curr_row += 1 # 게임 간 간격

    # --- 컬럼 너비 설정 ---
    _set_column_widths(ws)

    measurements = [(name, summary.get("measurement")) for name, summary in results_summary.items()
                    if summary.get("measurement")]
    if measurements:
        evidence = wb.create_sheet("Measurement evidence")
        evidence.append(["Capture", "Metric", "Value"])
        for name, fields in measurements:
            for key, value in fields.items():
                evidence.append([name, key, value])
                for cell in evidence[evidence.max_row]:
                    if isinstance(cell.value, str):
                        cell.data_type = "s"
        evidence.freeze_panes = "A2"
        for column, width in (("A", 36), ("B", 32), ("C", 70)):
            evidence.column_dimensions[column].width = width

    wb.save(save_path)

def _get_excel_styles():
    return {
        "header_fill_blue": PatternFill(start_color="002060", end_color="002060", fill_type="solid"),
        "header_fill_light": PatternFill(start_color="B4C6E7", end_color="B4C6E7", fill_type="solid"),
        "header_font_white": Font(color="FFFFFF", bold=True, size=16),
        "header_font_black": Font(bold=True, size=10),
        "data_font": Font(size=9),
        "link_font": Font(color="0000FF", underline="single", size=9),
        "alignment_center": Alignment(horizontal="center", vertical="center", wrap_text=True),
        "alignment_left": Alignment(horizontal="left", vertical="center"),
        "thin_border": Border(
            left=Side(style='thin'), 
            right=Side(style='thin'), 
            top=Side(style='thin'), 
            bottom=Side(style='thin')
        )
    }

def _group_by_game(results_summary: Dict) -> Dict:
    games = {}
    for f, s in results_summary.items():
        meta = s.get("metadata", {})
        g_name = meta.get('game_name') or "Unknown"
        p_name = meta.get('package_name') or "Unknown"
        game_key = f"{g_name} - {p_name}"
        if game_key not in games:
            games[game_key] = []
        games[game_key].append((f, s))
    return games

def _group_by_model(items: List) -> Dict:
    model_groups = {}
    for f, s in items:
        model = s.get("metadata", {}).get("model", "Unknown")
        if model not in model_groups:
            model_groups[model] = []
        model_groups[model].append((f, s))
    return model_groups

def _write_game_title(ws, row, game_name, col_count, styles):
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=col_count)
    cell = ws.cell(row=row, column=1, value=f"[{game_name}]")
    cell.fill = styles["header_fill_blue"]
    cell.font = styles["header_font_white"]
    cell.alignment = styles["alignment_left"]

def _write_headers(ws, row, headers_top, headers_bottom, top_merges, styles):
    for c, (ht, hb) in enumerate(zip(headers_top, headers_bottom), 1):
        c1 = ws.cell(row=row, column=c, value=ht)
        c2 = ws.cell(row=row + 1, column=c, value=hb)
        for cell in (c1, c2):
            cell.fill = styles["header_fill_light"]
            cell.font = styles["header_font_black"]
            cell.alignment = styles["alignment_center"]
            cell.border = styles["thin_border"]

    # 상단 병합 적용
    for start, end, text in top_merges:
        ws.merge_cells(start_row=row, start_column=start, end_row=row, end_column=end)
    
    # 상하 내용 동일하거나 하단이 공란일 경우 수직 병합
    for c in range(1, len(headers_top) + 1):
        t_val = ws.cell(row=row, column=c).value
        # headers_bottom 리스트를 직접 참조하거나 셀 값을 읽음
        b_val = ws.cell(row=row+1, column=c).value
        
        # 하단이 비어있거나 상하 내용이 같으면 병합
        if not b_val or str(b_val).strip() == "" or (t_val == b_val):
            ws.merge_cells(start_row=row, start_column=c, end_row=row+1, end_column=c)

def _write_data_row(ws, row, f, s, results_thermal, styles):
    meta = s.get("metadata", {})
    fdir = os.path.dirname(f)
    model = meta.get("model", "Unknown")
    
    # Thermal 데이터 매칭
    th = None
    for tf, tv in results_thermal.items():
        if os.path.dirname(tf) == fdir:
            th = tv
            break
    
    act_time = s.get("actual_measurement_time", (0, 0))
    duration_str = f"{act_time[0]}m {act_time[1]}s" if any(act_time) else ""

    # Dumpstate 정보가 있으면 우선 사용
    dump_info = meta.get("dumpstate", {})
    
    excel_model = model
    excel_sw_ver = meta.get("sw_ver", "")
    excel_os_ver = meta.get("os_ver", "")
    excel_hw_ver = ""
    excel_gos_ver = ""
    
    if dump_info:
        # User requirement: Model = "'Code' ('Model')"
        code = dump_info.get("Code", "")
        m_name = dump_info.get("Model", "")
        if code != "N/A" and m_name != "N/A":
            excel_model = f"{code} ({m_name})"
        
        # User requirement: SW ver = "'BuildNumber'"의 우측 끝 4글자
        build = dump_info.get("BuildNumber", "")
        if build != "N/A":
            excel_sw_ver = build[-4:] if len(build) >= 4 else build
            
        # User requirement: OS ver = "'Release'"
        release = dump_info.get("Release", "")
        if release != "N/A":
            excel_os_ver = release
            
        # User requirement: HW ver = "'HWver'"
        hw = dump_info.get("HWver", "")
        if hw != "N/A":
            excel_hw_ver = hw
            
        # User requirement: GOS ver = "GOS ver"
        gos = dump_info.get("GOS ver", "")
        if gos != "N/A":
            excel_gos_ver = gos

    # 데이터 매핑 (UI Summary 테이블과 동일 형식)
    row_data = [
        excel_model,
        excel_sw_ver,
        excel_os_ver,
        excel_hw_ver,
        excel_gos_ver,
        "", # Mode (추후 필요시 보완)
        meta.get("game_ver", ""),
        _format_report_date(meta.get("date", "")),
        duration_str,
        s.get("max_fps", ""),
        f"{s['median']} ({s['median_stability']}%)" if s.get('median') is not None else "",
        f"{s['average']} ({s['average_stability']}%)" if s.get('average') is not None else "",
        f"{s['first_half']} ({s['first_half_stability']}%)" if s.get('first_half') is not None else "",
        f"{s['second_half']} ({s['second_half_stability']}%)" if s.get('second_half') is not None else "",
        th.get("start_temp", "") if th else "",
        th.get("end_temp", "") if th else "",
        th.get("max_temp", "") if th else "",
        s.get("power", ""),
        "",
        os.path.basename(f)
    ]

    
    for c, val in enumerate(row_data, 1):
        cell = ws.cell(row=row, column=c, value=val)
        cell.border = styles["thin_border"]
        cell.alignment = styles["alignment_center"]
        cell.font = styles["data_font"]
        if c == 19: # Link 컬럼
            cell.hyperlink = f'file:///{fdir.replace("\\", "/")}'
            cell.font = styles["link_font"] 

def _set_column_widths(ws):
    col_widths = {
        'A': 13.8, 'B': 7.13, 'C': 7.13, 'D': 7.13, 'E': 8.38, 'F': 10, 'G': 12.8,
        'H': 9, 'I': 10, 'J': 10, 'K': 10, 'L': 10, 'M': 10, 'N': 10,
        'O': 10, 'P': 10, 'Q': 10, 'R': 10, 'S': 9, 'T': 32
    }
    for col, width in col_widths.items():
        ws.column_dimensions[col].width = width
