import asyncio
import io
import json
import os
from datetime import datetime

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
from openpyxl import Workbook

from automation import run_single_order

load_dotenv(os.path.expanduser('~/easygo-order-bot/.env'))

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


@app.route('/api/run', methods=['POST'])
def api_run():
    data = request.json or {}
    order_ids = [o.strip().upper() for o in data.get('order_ids', '').strip().splitlines() if o.strip()]
    # 每個人用自己的 be2 帳密登入（分享給組員用時，不用共用同一組），介面上沒填才退回 .env 的預設值。
    be2_username = data.get('be2_username', '').strip() or BE2_USERNAME
    be2_password = data.get('be2_password', '').strip() or BE2_PASSWORD
    if not order_ids:
        return jsonify({'error': '請輸入至少一筆 KKday 訂單編號'}), 400
    if not (be2_username and be2_password and EASYGO_PASSWORD):
        return jsonify({'error': '請輸入你的 be2 帳號密碼，並確認 .env 有設定 EASYGO_PASSWORD'}), 400

    results = []
    for order_id in order_ids:
        prog = []

        def push(msg, status='info', _prog=prog, _oid=order_id):
            _prog.append({'msg': msg, 'status': status})
            print(f'[{_oid}][{status}] {msg}')

        try:
            r = asyncio.run(
                run_single_order(order_id, be2_username, be2_password, EASYGO_USERNAME, EASYGO_PASSWORD, push)
            )
        except Exception as e:
            push(f'未預期錯誤：{e}', 'error')
            r = {'success': False, 'skipped': False, 'order_id': order_id, 'error': str(e)}

        result = {**r, 'progress': prog}
        results.append(result)
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
            }
        )

    succeeded = [r for r in results if r.get('success')]
    skipped = [r for r in results if r.get('skipped')]
    failed = [r for r in results if not r.get('success') and not r.get('skipped')]
    return jsonify(
        {
            'success': len(failed) == 0,
            'summary': {
                'total': len(results),
                'succeeded': len(succeeded),
                'skipped': len(skipped),
                'failed': len(failed),
            },
            'results': results,
        }
    )


@app.route('/api/export_results', methods=['POST'])
def export_results():
    data = request.json or {}
    results = data.get('results', [])
    wb = Workbook()
    ws = wb.active
    ws.title = '執行紀錄'
    ws.append(['KKday訂單編號', '狀態', 'EasyGo訂單ID', '說明', '導覽語系', '旅客備註'])
    for r in results:
        status = '成功' if r.get('success') else ('中止' if r.get('skipped') else '失敗')
        note = r.get('reason', '') or r.get('error', '')
        ws.append([
            r.get('order_id', ''),
            status,
            r.get('easygo_order_id', ''),
            note,
            r.get('nav_lang', '') or '',
            r.get('passenger_note', '') or '',
        ])
    for col in ws.columns:
        max_len = max((len(str(c.value or '')) for c in col), default=8)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 60)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = f'EasyGo訂購執行紀錄_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx'
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
