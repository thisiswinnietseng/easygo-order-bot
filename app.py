import asyncio
import io
import json
import os
from datetime import datetime

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
from openpyxl import Workbook

from automation import discover_order_candidates, run_single_order

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

app = Flask(__name__, static_folder='.', static_url_path='')
CORS(app)

BE2_USERNAME = os.environ.get('BE2_USERNAME', '')
BE2_PASSWORD = os.environ.get('BE2_PASSWORD', '')
EASYGO_USERNAME = os.environ.get('EASYGO_USERNAME', 'KKDAYJP')
EASYGO_PASSWORD = os.environ.get('EASYGO_PASSWORD', '')

HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'history.json')


def load_history():
    if not os.path.exists(HISTORY_FILE):
        return []
    with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_record(record):
    history = load_history()
    history.append(record)
    with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


@app.route('/')
def index():
    return app.send_static_file('index.html')


def _save_history_record(order_id, r):
    save_record(
        {
            'timestamp': datetime.now().strftime('%Y-%m-%dT%H:%M:%S'),
            'order_id': order_id,
            'success': r.get('success', False),
            'skipped': r.get('skipped', False),
            'reason': r.get('reason', r.get('error', '')),
            'easygo_order_id': r.get('easygo_order_id', ''),
            'nav_lang': r.get('nav_lang', ''),
            'passenger_note': r.get('passenger_note', ''),
            'kkday_product_id': r.get('kkday_product_id', ''),
            'kkday_product_name': r.get('kkday_product_name', ''),
            'kkday_package_name': r.get('kkday_package_name', ''),
            'passenger_spec': r.get('passenger_spec', ''),
        }
    )


def _summary_response(results, extra=None):
    succeeded = [r for r in results if r.get('success')]
    skipped = [r for r in results if r.get('skipped')]
    failed = [r for r in results if not r.get('success') and not r.get('skipped')]
    payload = {
        'success': len(failed) == 0,
        'summary': {
            'total': len(results),
            'succeeded': len(succeeded),
            'skipped': len(skipped),
            'failed': len(failed),
        },
        'results': results,
    }
    if extra:
        payload.update(extra)
    return jsonify(payload)


@app.route('/api/run', methods=['POST'])
def api_run():
    data = request.json or {}
    order_ids = [o.strip().upper() for o in data.get('order_ids', '').strip().splitlines() if o.strip()]
    # 帳密都從 .env 讀（每個人在自己電腦的 .env 填自己的 be2 帳密），網頁上不用填。
    if not order_ids:
        return jsonify({'error': '請輸入至少一筆 KKday 訂單編號'}), 400
    if not (BE2_USERNAME and BE2_PASSWORD and EASYGO_PASSWORD):
        return jsonify({'error': '請先在 .env 填好 BE2_USERNAME / BE2_PASSWORD / EASYGO_PASSWORD'}), 400

    results = []
    for order_id in order_ids:
        prog = []

        def push(msg, status='info', _prog=prog, _oid=order_id):
            _prog.append({'msg': msg, 'status': status})
            print(f'[{_oid}][{status}] {msg}')

        try:
            r = asyncio.run(
                run_single_order(order_id, BE2_USERNAME, BE2_PASSWORD, EASYGO_USERNAME, EASYGO_PASSWORD, push)
            )
        except Exception as e:
            push(f'未預期錯誤：{e}', 'error')
            r = {'success': False, 'skipped': False, 'order_id': order_id, 'error': str(e)}

        result = {**r, 'progress': prog}
        results.append(result)
        _save_history_record(order_id, r)

    return _summary_response(results)


@app.route('/api/discover_and_run', methods=['POST'])
def api_discover_and_run():
    # 自動搜尋模式：不用貼訂單編號，依 product_mapping.json 每個商品編號去 be2 搜尋符合
    # 條件（處理中＋旅客資料已齊全）的訂單，找到對照表就直接下單付款，找不到就跳過記錄。
    if not (BE2_USERNAME and BE2_PASSWORD and EASYGO_PASSWORD):
        return jsonify({'error': '請先在 .env 填好 BE2_USERNAME / BE2_PASSWORD / EASYGO_PASSWORD'}), 400

    discovery_prog = []

    def discovery_push(msg, status='info', _prog=discovery_prog):
        _prog.append({'msg': msg, 'status': status})
        print(f'[discover][{status}] {msg}')

    try:
        candidates = asyncio.run(discover_order_candidates(BE2_USERNAME, BE2_PASSWORD, discovery_push))
    except Exception as e:
        discovery_push(f'未預期錯誤：{e}', 'error')
        return jsonify({'error': f'搜尋過程發生未預期錯誤：{e}', 'progress': discovery_prog}), 500

    results = []
    for c in candidates:
        order_id = c['order_id']
        prog = list(discovery_prog)

        def push(msg, status='info', _prog=prog, _oid=order_id):
            _prog.append({'msg': msg, 'status': status})
            print(f'[{_oid}][{status}] {msg}')

        if c['jph_mapping'] is None:
            push(f"跳過：{c['skip_reason']}", 'skip')
            r = {
                'success': False,
                'skipped': True,
                'order_id': order_id,
                'reason': c['skip_reason'],
                'kkday_product_id': c.get('kkday_product_id', ''),
            }
        else:
            try:
                r = asyncio.run(
                    run_single_order(
                        order_id, BE2_USERNAME, BE2_PASSWORD, EASYGO_USERNAME, EASYGO_PASSWORD,
                        push, jph_mapping=c['jph_mapping'],
                    )
                )
            except Exception as e:
                push(f'未預期錯誤：{e}', 'error')
                r = {'success': False, 'skipped': False, 'order_id': order_id, 'error': str(e)}

        result = {**r, 'progress': prog}
        results.append(result)
        _save_history_record(order_id, r)

    return _summary_response(results, extra={'discovery_progress': discovery_prog})


def _build_results_xlsx(results, with_timestamp=False):
    wb = Workbook()
    ws = wb.active
    ws.title = '執行紀錄'
    header = [
        'KKday訂單編號', '狀態', 'JPH訂單ID', '說明', '導覽語系', '旅客備註',
        'KKday商品名稱', 'KKday商品編號', 'KKday套餐名稱', '旅客資料規格',
    ]
    if with_timestamp:
        header = ['執行時間'] + header
    ws.append(header)
    for r in results:
        status = '成功' if r.get('success') else ('中止' if r.get('skipped') else '失敗')
        note = r.get('reason', '') or r.get('error', '')
        row = [
            r.get('order_id', ''),
            status,
            r.get('easygo_order_id', ''),
            note,
            r.get('nav_lang', '') or '',
            r.get('passenger_note', '') or '',
            r.get('kkday_product_name', '') or '',
            r.get('kkday_product_id', '') or '',
            r.get('kkday_package_name', '') or '',
            r.get('passenger_spec', '') or '',
        ]
        if with_timestamp:
            row = [r.get('timestamp', '')] + row
        ws.append(row)
    for col in ws.columns:
        max_len = max((len(str(c.value or '')) for c in col), default=8)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 60)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


@app.route('/api/export_results', methods=['POST'])
def export_results():
    data = request.json or {}
    results = data.get('results', [])
    buf = _build_results_xlsx(results)
    filename = f'JPH訂購執行紀錄_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx'
    return send_file(
        buf,
        download_name=filename,
        as_attachment=True,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )


@app.route('/api/export_history')
def export_history():
    # 匯出從有這支工具以來、伺服器上累積的完整執行紀錄（history.json），
    # 不是只有目前畫面上這一批，重新整理頁面也不會遺失。
    # 支援用 ?start=YYYY-MM-DD&end=YYYY-MM-DD 篩選日期範圍，不帶就是匯出全部。
    start_date = request.args.get('start', '').strip()
    end_date = request.args.get('end', '').strip()
    history = load_history()
    if start_date:
        history = [r for r in history if r.get('timestamp', '')[:10] >= start_date]
    if end_date:
        history = [r for r in history if r.get('timestamp', '')[:10] <= end_date]
    buf = _build_results_xlsx(history, with_timestamp=True)
    filename = f'JPH訂購完整歷史紀錄_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx'
    return send_file(
        buf,
        download_name=filename,
        as_attachment=True,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )


@app.route('/api/history')
def api_history():
    return jsonify(load_history())


@app.route('/api/health')
def health():
    return jsonify({'ok': True})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5050, debug=False)
