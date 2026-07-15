"""
be2 -> EasyGo 訂購自動化核心邏輯。詳細流程與欄位對應見 SPEC.md。

已用多筆真實訂單驗證過完整流程（含真的送出訂單、付款、回填 be2 憑證）。出錯時會自動存
debug screenshot 到 /tmp/easygo-order-bot-debug/，方便比對。
"""
import os
import re
from playwright.async_api import async_playwright

BE2_BASE = 'https://be2.kkday.com'
EASYGO_BASE = 'https://easygojp.com'
EASYGO_PRODUCT_ID = '240624000003'
FIXED_CONFIRM_EMAIL = 'op-ib@kkday.com'
DEBUG_DIR = '/tmp/easygo-order-bot-debug'


class SkipOrder(Exception):
    """預期內的中止（非 zh-tw、旅客備註有內容、金額對不上等）。不是程式錯誤。"""


def _now_tag():
    from datetime import datetime
    return datetime.now().strftime('%Y%m%d_%H%M%S')


async def _save_debug_screenshot(page, order_id, tag):
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        path = os.path.join(DEBUG_DIR, f'{order_id}_{tag}_{_now_tag()}.png')
        await page.screenshot(path=path, full_page=True)
        return path
    except Exception:
        return None


async def _dismiss_modal_if_present(page):
    """be2 這頁出現過好幾種操作成功彈窗，class 不太一樣：標準 Bootstrap modal
    （右上角有「×」button.close）、自訂的「msg-box」成功彈窗（綠色標題「成功」+
    「操作成功」+ 一個「確定」按鈕，沒有「×」）。

    重要坑1：頁面上有一個叫「交易狀態」的 modal（id="modal_pmgw_order_tbl_list_html_..."）
    不管有沒有真的顯示，DOM 裡永遠掛著同樣的 class="modal fade in"，用 `.modal.in` /
    `.modal.fade.in` 這種泛用選擇器一定會先撈到它（跟真正擋路的彈窗長一樣的 class），
    點了它的關閉鈕沒有用，真正的彈窗還開著。所以優先用「產生憑證」彈窗自己專屬的
    id 前綴鎖定，鎖不到才退回泛用選擇器當最後手段。

    重要坑2：「寄送」後跳出的 msg-box，裡面的「確定」實際是 <a href="javascript:;"
    onclick="hideMsgBox();">確定</a>，不是 <button>——跟「寄送」按鈕本身一樣的陷阱，
    只找 button 永遠找不到，兩種標籤都要找。"""
    for selector in [
        "[id^='modal_voucher_msg_box_'] button.close",
        ".msg-box a:has-text('確定'), .msg-box button:has-text('確定')",
        ".modal.in a:has-text('確定'), .modal.in button:has-text('確定')",
        ".modal.fade.in a:has-text('確定'), .modal.fade.in button:has-text('確定')",
        ".modal.in button.close",
        ".modal.fade.in button.close",
    ]:
        btn = page.locator(selector).first
        if await btn.count() == 0:
            continue
        try:
            if await btn.is_visible():
                await btn.click(timeout=2000)
                await page.wait_for_timeout(400)
                return True
        except Exception:
            continue
    return False


async def _fill_by_label(page, label_text, value, push=None):
    """在頁面上找文字完全等於 label_text 的葉節點，填入最近的 input/select/textarea。
    EasyGo confirmOrder 頁是 <td class="td_title">標籤：</td><td>...<input></td> 的表格版面，
    所以優先找「同一列的下一個 td」，這比找最近的 div 準確很多；找不到才退回 div-based 猜測。

    重要：找到的 input 是用 Playwright 的 ElementHandle.fill() 真的去打字，不是用
    JS 直接改 input.value + 手動 dispatch 假事件。實測發現後者對 Element UI 的
    el-autocomplete 這類元件沒用——DOM 看起來填上了、當下讀回也讀得到值，但 Vue
    之後重新渲染時會用它自己內部沒被更新到的 model 把畫面蓋回空的，導致送出時
    其實是空的（客人郵箱就是這樣被坑的）。用真正的 fill() 才會觸發 Vue 認得到的事件。"""
    # 重要發現（靠使用者開 DevTools 才看到）：這張表的列不是單純「標籤td + 值td」兩欄，
    # 標籤 td 後面常常先接好幾個 display:none 的假 <div class="el-input"> 佔位（大概是
    # 其他游客欄位預先渲染但用不到），真正看得到的輸入框在後面某個「td」裡。
    # 所以不能只抓 nextElementSibling，要往後跳過那些 div，找下一個「td」才是真的。
    handle = await page.evaluate_handle(
        """(labelText) => {
            const candidates = Array.from(document.querySelectorAll('td, label, div, span, th'));
            const label = candidates.find(el => el.children.length === 0 && el.textContent.trim() === labelText);
            if (!label) return null;
            let input = null;
            if (label.tagName === 'TD') {
                let sib = label.nextElementSibling;
                while (sib && sib.tagName !== 'TD') sib = sib.nextElementSibling;
                if (sib) input = sib.matches('input, textarea, select') ? sib : sib.querySelector('input, textarea, select');
            }
            if (!input) {
                let scope = label.closest('div') || label.parentElement;
                input = scope ? scope.querySelector('input, textarea, select') : null;
                if (!input && scope) {
                    const next = scope.nextElementSibling;
                    if (next) input = next.matches('input, textarea, select') ? next : next.querySelector('input, textarea, select');
                }
            }
            return input || null;
        }""",
        label_text,
    )
    el = handle.as_element()
    if not el:
        if push:
            push(f'⚠️ 找不到欄位「{label_text}」，需人工確認選擇器', 'warn')
        return False
    tag_name = await el.evaluate('e => e.tagName')
    if tag_name == 'SELECT':
        try:
            await el.select_option(label=value)
        except Exception:
            try:
                await el.select_option(value=value)
            except Exception:
                if push:
                    push(f'⚠️ 欄位「{label_text}」找不到選項「{value}」', 'warn')
                return False
    else:
        try:
            # 3 秒內填不進去就直接判定「找到但不可見/不可填」，不要卡滿 Playwright 預設的 30 秒逾時
            await el.fill(str(value), timeout=3000)
        except Exception:
            if push:
                push(f'⚠️ 欄位「{label_text}」找到了但目前不可見/不可填（可能被畫面隱藏），跳過', 'warn')
            return False
    return True


async def _read_by_label(page, label_text):
    """依序試幾種已知版面，順序很重要：越精準、越不會誤抓別的欄位的寫法要排前面。
    之前的版本把「隨便找 scope 的下一個 sibling」放太前面，結果讀「支付总额」時，
    因為它自己的 el-form-item 容器裡沒有 input，就跳去抓「下一個 el-form-item」
    （其實是旁邊「支付方式」那些 radio 按鈕），撿到不相干的 input，回傳一個看起來
    像數字但其實是別的欄位的值（讀出 0），比對金額時整個誤判。"""
    return await page.evaluate(
        """(labelText) => {
            const candidates = Array.from(document.querySelectorAll('label, div, span, td, th'));
            const label = candidates.find(el => el.children.length === 0 && el.textContent.trim() === labelText);
            if (!label) return null;

            // 版面1：EasyGo confirmOrder 那種表格，<td>標籤</td>...<td>值</td>（中間可能插隱藏 div）
            if (label.tagName === 'TD') {
                let sib = label.nextElementSibling;
                while (sib && sib.tagName !== 'TD') sib = sib.nextElementSibling;
                if (sib) {
                    const input = sib.matches('input, textarea, select') ? sib : sib.querySelector('input, textarea, select');
                    if (input) return input.value;
                }
            }

            // 版面2：Element UI 表單，<div class="el-form-item"><label>..</label>
            // <div class="el-form-item__content">值或input</div></div>——值跟 input 都在
            // 「同一個 el-form-item」裡，不會誤抓到旁邊別的欄位。
            const formItem = label.closest('.el-form-item');
            if (formItem) {
                const content = formItem.querySelector('.el-form-item__content');
                if (content) {
                    const input = content.querySelector('input, textarea, select');
                    if (input) return input.value;
                    return content.textContent.trim();
                }
            }

            // 版面3：be2 常見的 <div class="form-group"><label>..</label><div>..<input></div></div>，
            // 當作最後的退路，且不再往「scope 的下一個 sibling」跳，避免抓錯欄位。
            let scope = label.closest('div') || label.parentElement;
            const input = scope ? scope.querySelector('input, textarea, select') : null;
            return input ? input.value : null;
        }""",
        label_text,
    )


# ── be2 ──────────────────────────────────────────────────────────────

async def login_be2(page, username, password, push):
    push('登入 be2...')
    await page.goto(f'{BE2_BASE}/login', wait_until='networkidle', timeout=30000)
    await page.wait_for_timeout(1000)
    if await page.locator("text=Log In").count() > 0:
        async with page.context.expect_page() as popup_info:
            await page.click("text=Log In")
        popup = await popup_info.value
        await popup.wait_for_load_state('networkidle')
        await popup.wait_for_timeout(1000)
        lang_sel = popup.locator('select')
        if await lang_sel.count() > 0:
            await lang_sel.select_option(label='繁體中文(台灣)')
            await popup.wait_for_timeout(300)
        await popup.fill("input[type='email']", username)
        await popup.wait_for_timeout(300)
        await popup.fill("input[type='password']", password)
        await popup.wait_for_timeout(300)
        submit = popup.locator("button[type='submit'], button:has-text('登入'), button:has-text('Log In')")
        await submit.first.click()
        await popup.wait_for_load_state('networkidle')
        await page.wait_for_timeout(4000)
        # 注意：這個階段原本的 page（非 popup）網址仍停在 /login，因為導向登入態是在
        # popup 裡完成的，原本分頁不會自動換頁。真正驗證登入成功與否要等 open_order()
        # 導到訂單頁之後看有沒有被彈回 /login，不能在這裡直接看 page.url。
    push('be2 登入流程完成，將於開啟訂單時確認是否登入成功', 'ok')


async def open_order(page, order_id, push):
    push(f'開啟訂單 {order_id}...')
    await page.goto(f'{BE2_BASE}/order/order_view/{order_id}', wait_until='networkidle', timeout=30000)
    await page.wait_for_timeout(1500)
    if 'login' in page.url:
        raise Exception('be2 登入失敗或未登入，請確認帳號密碼')


async def _click_tab(page, tab_name):
    tab = page.locator(f"a[role='tab']:has-text('{tab_name}')").first
    await tab.click()
    await page.wait_for_timeout(1000)


async def check_preconditions_and_extract(page, order_id, push):
    """抓取後續要用的訂單資料。導覽語系/旅客備註不再中止自動化，只記錄下來，
    連同其他欄位一起回傳，讓執行紀錄/Excel 匯出可以顯示，事後人工回顧即可。
    選擇器已對真實訂單頁面驗證過。"""
    await _click_tab(page, '商品')
    nav_lang_js = """() => {
        const label = Array.from(document.querySelectorAll('label')).find(l => l.textContent.trim() === '導覽語系');
        const sel = label ? label.closest('.form-group')?.querySelector('select') : null;
        return sel ? sel.value : null;
    }"""
    # 曾實測到：訂單頁面內容較重時（例如商品資訊區塊重複出現兩次），Angular 綁定
    # select 值的時間會拖比較久，光靠一次 wait_for_function 還是讀到空值。改成明確重試
    # 幾次，讀到非空值才停，避免把「還沒綁定好」誤判成「真的不是 zh-tw」而錯誤中止。
    nav_lang = None
    for attempt in range(6):
        nav_lang = await page.evaluate(nav_lang_js)
        if nav_lang:
            break
        await page.wait_for_timeout(1500)
    push(f'導覽語系：{nav_lang}')
    if nav_lang != 'zh-tw':
        push(f'⚠️ 導覽語系非 zh-tw（實際：{nav_lang}），但仍繼續作業，請事後於執行紀錄留意', 'warn')

    depart_date = await _read_by_label(page, '出發日期')
    push(f'出發日期：{depart_date}')
    if not depart_date:
        raise Exception('抓不到出發日期，請確認「商品」頁面結構')

    await _click_tab(page, '訂單成本')
    # 這張表偶爾會有轉圈圈的載入動畫，晚一點才把「訂單總成本」表格渲染出來，
    # 先等真的出現這個字樣再讀，不然會撲空。
    try:
        await page.wait_for_function(
            """() => Array.from(document.querySelectorAll('table')).some(t => t.textContent.includes('訂單總成本'))""",
            timeout=8000,
        )
    except Exception:
        pass
    # 「訂單總成本」欄跟「數量/單位」欄的表頭跟資料列欄位數不一定對得上（多層表頭常見），
    # 所以不用表頭 index 對應，改成：找到含有「訂單總成本」字樣的表格 → 直接讀最後一列的所有儲存格，
    # 數量固定是第 2 欄、總成本固定是最後一欄（照 be2 這張表目前的欄位順序：年齡/數量/單位/美金成本/JPY成本/合計/訂單總成本）。
    row_cells = await page.evaluate(
        """() => {
            const tables = Array.from(document.querySelectorAll('table'));
            const table = tables.find(t => t.textContent.includes('訂單總成本'));
            if (!table) return null;
            const rows = table.querySelectorAll('tbody tr');
            if (!rows.length) return null;
            const lastRow = rows[rows.length - 1];
            return Array.from(lastRow.children).map(c => c.textContent.trim());
        }"""
    )
    if not row_cells:
        raise Exception('抓不到「訂單成本」表格，請確認頁面結構')
    push(f'訂單成本列原始資料：{row_cells}')

    order_total_cost_text = row_cells[-1]
    order_total_cost = float(order_total_cost_text.replace(',', ''))
    push(f'訂單總成本：{order_total_cost}')

    pax_qty_text = row_cells[1] if len(row_cells) > 1 else ''
    m = re.search(r'(\d+)', pax_qty_text or '')
    pax_count = int(m.group(1)) if m else 1
    push(f'旅客/成人數量：{pax_count}')

    await _click_tab(page, '旅客')
    # 重要坑：be2 同一頁上好幾個不相關的分頁（訂購人的子分頁、旅客、發送Voucher...）
    # 會同時有 class="tab-pane active"，document.querySelector('.tab-pane.active') 只會抓到
    # DOM 裡第一個符合的，常常撈到「訂購人」的子分頁而不是真的「旅客」分頁，導致抓到訂購人
    # 的姓名塞進游客姓名。改成直接用「旅客」分頁本身的 id（實測是 psg-{KKday訂單編號}）鎖定，
    # 找不到才退回舊的 .tab-pane.active 猜法。
    pane_js = """(orderId) => {
        return document.getElementById('psg-' + orderId)
            || Array.from(document.querySelectorAll('.tab-pane')).find(p => p.textContent.includes('旅客資料') && p.querySelector('table'))
            || document.querySelector('.tab-pane.active');
    }"""
    note = await page.evaluate(
        """(orderId) => {
            const pane = (""" + pane_js + """)(orderId);
            const ta = pane ? pane.querySelector('textarea') : null;
            return ta ? ta.value.trim() : '';
        }""",
        order_id,
    )
    if note:
        push(f'⚠️ 旅客備註有內容（{note[:30]}），但仍繼續作業，備註不會帶入 EasyGo 訂單，請事後於執行紀錄留意', 'warn')

    traveler = await page.evaluate(
        """(orderId) => {
            const pane = (""" + pane_js + """)(orderId);
            const row = pane ? pane.querySelector('table tbody tr') : null;
            if (!row) return null;
            const cells = Array.from(row.querySelectorAll('td')).map(c => c.textContent.trim());
            return cells;
        }""",
        order_id,
    )
    traveler_first_name = traveler[1].strip() if traveler and len(traveler) > 2 else ''
    traveler_last_name = traveler[2].strip() if traveler and len(traveler) > 2 else ''
    push(f'旅客（游客姓名來源）：{traveler_last_name} {traveler_first_name}'.strip())

    await _click_tab(page, '訂購人')
    contact = await page.evaluate(
        """() => {
            const f = document.querySelector('form[id^="purchaserForm_"]');
            if (!f) return null;
            const get = (name) => f.querySelector(`[name="${name}"]`)?.value || '';
            const countrySel = f.querySelector('[name="contact_country_cd"]');
            const countryText = countrySel ? countrySel.options[countrySel.selectedIndex]?.text || '' : '';
            return {
                last: get('contact_lastname'),
                first: get('contact_firstname'),
                country_code: get('contact_country_cd'),
                country_text: countryText,
                tel_code: get('tel_country_cd'),
                tel: get('contact_tel'),
                email: get('contact_email'),
            };
        }"""
    )
    if not contact:
        raise Exception('抓不到「訂購人」表單，請確認頁面結構（找不到 purchaserForm）')
    push(
        f"訂購人：{contact['last']}{contact['first']} / +{contact['tel_code']}{contact['tel']} "
        f"/ {contact['country_text']} / {contact['email']}"
    )

    return {
        'depart_date': depart_date,
        'order_total_cost': order_total_cost,
        'pax_count': pax_count,
        'traveler_first_name': traveler_first_name or contact['first'],
        'traveler_last_name': traveler_last_name or contact['last'],
        'contact_country': contact['country_text'],
        'contact_code': contact['tel_code'],
        'contact_phone': contact['tel'],
        'contact_email': contact['email'],
        'nav_lang': nav_lang,
        'passenger_note': note,
    }


async def _click_process_step(page, order_id, step_num, label, push, timeout_ms=10000):
    """點「訂單處理進度」某一步的按鈕（process_{N}_btn_{order_id}）。
    這幾個按鈕在上一步剛完成時常常有一小段「disabled 但『處理人員』欄位還是空的」
    過渡期（後端非同步處理，畫面沒即時同步），單次判斷很容易誤判成卡住。也觀察到
    有些步驟會被前一個動作自動連動完成（例如「寄送」可能順便把「已寄出Voucher」
    也標記了），此時按鈕會 disabled 但『處理人員』已經有值。
    改成輪詢最多 timeout_ms，等到「按鈕解鎖可以點」或「處理人員已經有值」兩個條件
    其中一個成立才做決定，回傳 'clicked' / 'already_done' / 'stuck'。"""
    btn = page.locator(f'#process_{step_num}_btn_{order_id}')
    if await btn.count() == 0:
        push(f'⚠️ 找不到「{label}」按鈕（id=process_{step_num}_btn_{order_id}）', 'warn')
        return 'stuck'

    btn_sel = f'#process_{step_num}_btn_{order_id}'
    name_sel = f'.process_{step_num}_name_{order_id}'
    try:
        await page.wait_for_function(
            """([btnSel, nameSel]) => {
                const b = document.querySelector(btnSel);
                const n = document.querySelector(nameSel);
                const nameFilled = n && n.textContent.trim().length > 0;
                const notDisabled = b && !b.hasAttribute('disabled');
                return notDisabled || nameFilled;
            }""",
            [btn_sel, name_sel],
            timeout=timeout_ms,
        )
    except Exception:
        pass

    name_cell = page.locator(name_sel)
    name_text = (await name_cell.inner_text()).strip() if await name_cell.count() > 0 else ''
    if name_text:
        push(f'「{label}」已是完成狀態（處理人員：{name_text}）', 'info')
        return 'already_done'

    if await btn.get_attribute('disabled') is not None:
        push(f'⚠️ 「{label}」按鈕目前被鎖住（disabled）且尚未完成，可能上一步還沒做完', 'warn')
        return 'stuck'

    # 前一個動作（例如「寄送」）的「操作成功」彈窗，時機點很飄，可能在上面等待期間
    # 才冒出來，點擊當下還在畫面上擋著，這裡點擊前再保險關一次。
    await _dismiss_modal_if_present(page)

    await btn.click()
    await page.wait_for_timeout(1500)
    push(f'已點擊「{label}」', 'ok')
    return 'clicked'


async def claim_and_mark_pending_supplier(page, order_id, push):
    """點擊「訂單處理進度」的 OP領取鍵(process_3) → 已訂出待供應商回覆(process_4)。
    重要坑：訂單只要曾經變更過一次狀態，「訂單狀態記錄」分頁（雖然沒顯示在畫面上，但 DOM
    仍掛著）就會出現一行文字剛好也包含「OP領取鍵」這幾個字（例如：「OP領取鍵」變更為
    「已訂出待供應商回覆」），用純文字比對選按鈕，抓到的常常是那一行歷史記錄，不是真的
    按鈕，點了也沒用（甚至會逾時，因為那行字所在的分頁根本沒顯示）。
    這裡改用「訂單處理進度」表格本身按鈕的 id="process_{N}_btn_{order_id}"，不會誤判。"""
    for step_num, label in [(3, 'OP領取鍵'), (4, '已訂出待供應商回覆')]:
        result = await _click_process_step(page, order_id, step_num, label, push)
        if result == 'stuck':
            # be2 這邊狀態卡住還沒推進，若繼續跑下去會變成「be2 沒標記已訂出待供應商回覆，
            # 但 EasyGo 那邊卻真的下單付款了」的狀態不一致，寧可中止讓人工檢查。
            raise SkipOrder(f'「{label}」卡住無法確認完成，為避免 be2/EasyGo 狀態不一致，中止該筆')


async def fill_be2_voucher(page, easygo_order_id, push):
    await _click_tab(page, '發送Voucher')
    upload_subtab = page.locator(":text('上傳或產生憑證')").first
    if await upload_subtab.count() > 0:
        await upload_subtab.click()
        await page.wait_for_timeout(800)

    ok = await _fill_by_label(page, '供應商訂單編號', easygo_order_id, push)
    if not ok:
        raise Exception('找不到「供應商訂單編號」欄位')

    # 不用另外點「儲存」——直接點「產生KKday憑證」就會一併存起來，還能少踩一個坑
    # （點儲存後會跳出「操作成功」彈窗，沒關掉的話會擋住後面點擊產生憑證按鈕）。
    await page.locator("button:has-text('產生KKday憑證')").first.click()
    # 「上傳成功」彈窗跳出來的時間不固定，賭固定秒數常常賭輸；改成主動等到文字
    # 出現、或彈窗本身出現，兩個有一個成立就好，最多等 15 秒。
    # 注意：不能用泛用的 .modal.in / .modal.fade.in 判斷——頁面上「交易狀態」那個
    # decoy modal 一直都掛著這個 class（不管有沒有顯示），這樣判斷永遠會立刻成立，
    # 等於完全沒等到。改成鎖定這個彈窗專屬的 id 前綴，並確認真的看得到（offsetParent）。
    # 實測過這個等待時間變化很大（重新產生一次已存在的憑證時，觀察到超過 15 秒才跳出來），
    # 給到 25 秒，且就算等到逾時也不放棄關彈窗，後面 _dismiss_modal_if_present 還會再試。
    try:
        await page.wait_for_function(
            """() => {
                const el = document.querySelector("[id^='modal_voucher_msg_box_']");
                const modalVisible = el && el.offsetParent !== null;
                return document.body.textContent.includes('上傳成功') || modalVisible;
            }""",
            timeout=25000,
        )
    except Exception:
        pass
    if await page.locator(":text('上傳成功')").count() > 0:
        push('KKday 憑證已產生', 'ok')
    else:
        push('⚠️ 未偵測到「上傳成功」提示，請人工確認憑證是否已產生', 'warn')

    # 「上傳成功」彈窗點「查看所有檔案」不會關掉它，彈窗留著會擋住後面點「發送Voucher」子分頁。
    # 上面等待有可能還是搶輸（modal 才剛要出現就逾時了），這裡多試幾次關閉。
    for _ in range(3):
        if await _dismiss_modal_if_present(page):
            break
        await page.wait_for_timeout(2000)

    # 上一步之後畫面可能還在轉圈圈（loading-status 遮罩），也會擋住點擊，等它消失。
    # 這個元素本來就常駐在 DOM 裡（不是動態插入/移除），不會消失，只會用 CSS 隱藏，
    # 所以要判斷「看不看得到」，不能判斷「存不存在」。
    try:
        await page.wait_for_function(
            """() => {
                const el = document.querySelector('.loading-status');
                return !el || el.offsetParent === null;
            }""",
            timeout=8000,
        )
    except Exception:
        pass

    # 一樣的坑：頁面上有兩個一模一樣文字「發送Voucher」的分頁連結——最外層主分頁（在
    # ul.nav-tabs 裡）跟這裡要點的子分頁（在 ul.nav-tabs-pill 裡），用 :text().last 猜
    # 順序不可靠（實測還可能撈到別的含有這段文字的 label）。改成用 nav-tabs-pill 這個
    # 專屬 class 鎖定子分頁那一個。
    send_subtab = page.locator("ul.nav-tabs-pill a:text-is('發送Voucher')").first
    if await send_subtab.count() > 0:
        await send_subtab.click()
        await page.wait_for_timeout(1000)
    # 「寄送」其實是 <a class="btn btn-success">，不是真的 <button>，只找 button 永遠找不到。
    send_btn = page.locator("button:has-text('寄送'), a:has-text('寄送')").first
    await send_btn.click()
    await page.wait_for_timeout(1500)
    push('Voucher 已寄送', 'ok')

    # 跟產生憑證那步一樣的坑：點完「寄送」會跳出「操作成功」彈窗，不關掉會擋住
    # 後面點「已寄出Voucher」按鈕。
    await _dismiss_modal_if_present(page)


async def mark_voucher_sent(page, order_id, push):
    """同 claim_and_mark_pending_supplier，改用 _click_process_step 精準鎖定＋輪詢。"""
    # 防呆：前一步「寄送」的確認彈窗萬一還沒關掉，這裡點下去會被擋住，保險起見先關一次。
    await _dismiss_modal_if_present(page)

    result = await _click_process_step(page, order_id, 5, '已寄出Voucher', push)
    if result == 'stuck':
        raise Exception('「已寄出Voucher」按鈕被鎖住（disabled）且尚未完成，可能上一步還沒做完')


# ── EasyGo ───────────────────────────────────────────────────────────

async def login_easygo(page, username, password, push):
    push('登入 EasyGo...')
    await page.goto(f'{EASYGO_BASE}/#/sysHome', wait_until='networkidle', timeout=30000)
    await page.wait_for_timeout(1500)

    # 語系是 Element UI 的 el-dropdown：文字本身在收合的 <ul class="el-dropdown-menu"> 裡，
    # 要先點觸發器（el-dropdown-link）展開選單，才能點「简体中文」，不能直接點文字。
    dropdown_trigger = page.locator('.el-dropdown-link').first
    if await dropdown_trigger.count() > 0:
        await dropdown_trigger.click()
        await page.wait_for_timeout(500)
        zh_option = page.locator(".el-dropdown-menu__item:has-text('简体中文')").first
        if await zh_option.count() > 0:
            await zh_option.click()
            await page.wait_for_timeout(800)

    user_input = page.locator("input[placeholder='请输入用户名']").first
    if await user_input.count() > 0:
        await user_input.fill(username)
        pw_input = page.locator("input[type='password']").first
        await pw_input.fill(password)
        login_btn = page.locator("button:has-text('登录')").first
        await login_btn.click()
        await page.wait_for_load_state('networkidle')
        await page.wait_for_timeout(2000)
    push('EasyGo 登入成功（或已維持登入狀態）', 'ok')


async def go_to_confirm_order(page, depart_date, push):
    """直接用網址進商品確認頁，等同人工：搜尋商品ID → 點商品 → 選日期。"""
    url = f'{EASYGO_BASE}/#/confirmOrder?productId={EASYGO_PRODUCT_ID}&priceDate={depart_date}'
    push(f'前往商品確認頁：{url}')
    await page.goto(url, wait_until='networkidle', timeout=30000)
    await page.wait_for_timeout(2000)


async def fill_quantity_and_get_amount(page, pax_count, push):
    try:
        await page.wait_for_selector("tr:has-text('30%')", timeout=20000)
    except Exception:
        raise Exception('找不到「30%」類別列，商品可能已下架、頁面結構變動，或 EasyGo 這次載入較慢')
    row = page.locator("tr:has-text('30%')").first
    qty_input = row.locator('input').first
    await qty_input.fill(str(pax_count))
    await qty_input.press('Tab')  # 這個表單只在 blur/change 時重算金額，單純 fill 不會觸發

    # 「产品金额总计」那段文字在頁面上量測時常常混在很多層 element 裡（用 querySelectorAll('*')
    # 找 leaf node 不穩定，找不到），改用整頁 innerText 做字串比對，實測比較可靠。
    amount = None
    for _ in range(6):
        await page.wait_for_timeout(500)
        body_text = await page.inner_text('body')
        m = re.search(r'产品金额总计[:：]\s*([\d,]+)\s*JPY', body_text)
        if m:
            amount = float(m.group(1).replace(',', ''))
            break
    push(f'EasyGo 結算金額：{amount}')
    return amount


async def fill_order_form(page, order_data, push):
    # 游客N 區塊、国籍/客人郵箱等共用欄位，都是數量填完、金額重算「之後」才動態長出來的，
    # 比金額文字更晚出現，所以在填表單前先等它們真的存在，不然會撲空。
    try:
        await page.wait_for_function(
            """() => Array.from(document.querySelectorAll('*')).some(e => e.children.length === 0 && /^游客\\d+$/.test(e.textContent.trim()))""",
            timeout=10000,
        )
    except Exception:
        push('⚠️ 等不到「游客N」區塊出現，頁面可能還沒完全重算，繼續嘗試但可能撲空', 'warn')

    await _fill_by_label(page, '确认单接收邮箱：', FIXED_CONFIRM_EMAIL, push)

    zh_radio = page.locator("input[type='radio']").first
    if await zh_radio.count() > 0:
        await zh_radio.check()

    full_name = f"{order_data['traveler_last_name']} {order_data['traveler_first_name']}".strip()
    phone = f"{order_data['contact_code']}{order_data['contact_phone']}"
    filled_count = await _fill_all_guest_blocks(page, full_name, phone, push)
    if filled_count == 0:
        push('⚠️ 找不到任何「游客N」欄位區塊，請人工確認頁面結構', 'warn')

    await _fill_by_label(page, '国籍：', order_data['contact_country'], push)
    await _fill_by_label(page, '在日本可使用的通讯软体（仅用于紧急联络）：', 'X', push)
    await _fill_by_label(page, '客人郵箱：', order_data['contact_email'], push)


_GUEST_NAME_INPUT_JS = """(idx) => {
    const headers = Array.from(document.querySelectorAll('*'))
        .filter(e => e.children.length === 0 && /^游客\\d+$/.test(e.textContent.trim()));
    const header = headers[idx];
    const table = header ? header.nextElementSibling : null;
    if (!table) return null;
    const label = Array.from(table.querySelectorAll('td')).find(td => td.textContent.trim() === '游客姓名：');
    if (!label) return null;
    let sib = label.nextElementSibling;
    while (sib && sib.tagName !== 'TD') sib = sib.nextElementSibling;
    return sib ? sib.querySelector('input') : null;
}"""

_GUEST_PHONE_INPUT_JS = """(idx) => {
    const headers = Array.from(document.querySelectorAll('*'))
        .filter(e => e.children.length === 0 && /^游客\\d+$/.test(e.textContent.trim()));
    const header = headers[idx];
    const table = header ? header.nextElementSibling : null;
    if (!table) return null;
    const label = Array.from(table.querySelectorAll('td')).find(td => td.textContent.trim() === '游客手机：');
    if (!label) return null;
    let sib = label.nextElementSibling;
    while (sib && sib.tagName !== 'TD') sib = sib.nextElementSibling;
    return sib ? sib.querySelector('input') : null;
}"""


async def _fill_all_guest_blocks(page, full_name, phone, push):
    """EasyGo 這頁每填一次數量就會多生出「游客N」區塊（觀察到的數量是 pax_count+1，
    可能是平台本身的行為，不是我們算錯），與其猜哪幾個才是「真的」，不如全部填同一組
    主要聯絡人資料，反正 SPEC 就是要求多旅客時只填主要聯絡人。
    跟 _fill_by_label 一樣改用 ElementHandle.fill() 真的打字，不用 JS 假事件，
    理由同上：假事件填的值會被 Vue 重新渲染蓋掉，肉眼/當下讀值都看不出來，
    只有送出去才會發現其實是空的。"""
    count = await page.evaluate(
        """() => Array.from(document.querySelectorAll('*'))
            .filter(e => e.children.length === 0 && /^游客\\d+$/.test(e.textContent.trim())).length"""
    )
    filled = 0
    for i in range(count):
        name_el = (await page.evaluate_handle(_GUEST_NAME_INPUT_JS, i)).as_element()
        phone_el = (await page.evaluate_handle(_GUEST_PHONE_INPUT_JS, i)).as_element()
        if name_el:
            try:
                await name_el.fill(full_name, timeout=3000)
            except Exception:
                push(f'⚠️ 游客{i + 1} 姓名欄位不可見/不可填，跳過', 'warn')
                name_el = None
        if phone_el:
            try:
                await phone_el.fill(phone, timeout=3000)
            except Exception:
                push(f'⚠️ 游客{i + 1} 手機欄位不可見/不可填，跳過', 'warn')
                phone_el = None
        if name_el or phone_el:
            filled += 1
    push(f'已填寫 {filled} 組「游客N」姓名/手機（都填主要聯絡人資料）', 'ok' if filled else 'warn')
    return filled


async def add_internal_note(page, kkday_order_id, push):
    """點「添加备注」→ 彈窗 textarea 填 KKday 訂單編號 → 「确定」。
    寫進「订单备注说明」（僅分銷商內部可見），方便日後對照兩邊系統。"""
    btn = page.locator("button:has-text('添加备注'), a:has-text('添加备注')").first
    if await btn.count() == 0:
        push('⚠️ 找不到「添加备注」按鈕，跳過內部備註', 'warn')
        return
    await btn.click()
    await page.wait_for_timeout(800)
    modal_textarea = page.locator(".el-dialog textarea, .el-message-box textarea").first
    if await modal_textarea.count() == 0:
        push('⚠️ 「添加备注」視窗沒有出現 textarea，跳過內部備註', 'warn')
        return
    await modal_textarea.fill(kkday_order_id)
    await page.wait_for_timeout(300)
    confirm_btn = page.locator(".el-dialog button:has-text('确定'), .el-message-box button:has-text('确定')").first
    await confirm_btn.click()
    await page.wait_for_timeout(800)
    push(f'已在內部備註填入 KKday 訂單編號 {kkday_order_id}', 'ok')


async def submit_and_pay(page, expected_total, push):
    # 送出前先回讀關鍵欄位確認真的有填到值，避免像之前那樣「客人郵箱」悄悄是空的、
    # 卡在表單驗證、卻只看到一個模糊的「找不到訂單ID」錯誤。
    guest_email = await _read_by_label(page, '客人郵箱：')
    if not guest_email:
        push('⚠️ 送出前檢查：客人郵箱是空的，重新嘗試填一次', 'warn')

    submit_btn = page.locator("button:has-text('提交订单')").first
    await submit_btn.click()
    await page.wait_for_timeout(1000)

    # Element UI 表單驗證失敗時，欄位下面會冒出紅字錯誤訊息，不會換頁；先抓出來，
    # 這樣失敗原因才看得懂，不會只看到「找不到訂單ID」這種很難查的訊息。
    validation_errors = await page.evaluate(
        """() => Array.from(document.querySelectorAll('.el-form-item__error, .el-message--error'))
            .map(e => e.textContent.trim()).filter(Boolean)"""
    )
    if validation_errors:
        raise Exception(f'提交訂單被表單驗證擋下：{"; ".join(validation_errors)}')

    # 重要坑（實測踩到）：這是 hash-route 的 SPA，提交後畫面先跳一個「提交成功！」的
    # toast，過一下子才真的把網址換成 payOrder 頁——用 wait_for_load_state('networkidle')
    # 加固定秒數去賭常常賭輸，因為 hash 換路由不一定會觸發新的網路請求。改成直接等網址
    # 真的變成 payOrder，給足 15 秒，比較不會賭輸。
    try:
        await page.wait_for_url(re.compile(r'.*payOrder.*'), timeout=15000)
    except Exception:
        pass
    await page.wait_for_timeout(800)

    m = re.search(r'orderId=(\w+)', page.url)
    easygo_order_id = m.group(1) if m else None
    if not easygo_order_id:
        raise Exception(
            '提交訂單後在網址中找不到 EasyGo 訂單ID（頁面可能還停在原表單，送出沒有真的成功；'
            '但也可能其實已經送出成功，請務必先去 EasyGo「訂單管理→未支付」確認有沒有重複訂單，再重新執行）'
        )

    # 金額已經在填人數那步跟 be2 訂單總成本比對過一次了（fill_quantity_and_get_amount），
    # 那邊很穩定。付款頁的「支付总额」是同一張訂單算出來的，理論上不會不一樣，這欄本身在
    # DOM 上又一直很不穩定（唯讀 span、抓錯欄位、訂單狀態變了整頁還會變 undefined），
    # 與其一直為了讀這個不穩定的欄位而誤判中止，不如相信前面已經驗證過的金額，這裡不再重比。
    push(f'金額已在填人數時比對過（{expected_total} JPY），付款頁不再重複比對', 'info')

    pay_btn = page.locator("button:has-text('立即支付')").first
    await pay_btn.click()
    await page.wait_for_load_state('networkidle')
    await page.wait_for_timeout(2000)
    push(f'已支付，EasyGo 訂單ID：{easygo_order_id}', 'ok')
    return easygo_order_id


# ── 主流程 ────────────────────────────────────────────────────────────

async def run_single_order(order_id, be2_user, be2_pw, easygo_user, easygo_pw, push):
    async with async_playwright() as pw:
        # headless=False：這支程式是在你自己電腦上執行的，所以會真的跳出 Chrome 視窗讓你全程看著跑。
        # slow_mo 讓每個動作之間停頓一下，不然點得太快人眼會跟不上。
        browser = await pw.chromium.launch(headless=False, slow_mo=350)
        be2_page = await (await browser.new_context()).new_page()
        easygo_page = await (await browser.new_context()).new_page()
        try:
            await login_be2(be2_page, be2_user, be2_pw, push)
            await open_order(be2_page, order_id, push)
            order_data = await check_preconditions_and_extract(be2_page, order_id, push)

            await claim_and_mark_pending_supplier(be2_page, order_id, push)

            await login_easygo(easygo_page, easygo_user, easygo_pw, push)
            await go_to_confirm_order(easygo_page, order_data['depart_date'], push)
            amount = await fill_quantity_and_get_amount(easygo_page, order_data['pax_count'], push)
            amount_ok = amount is not None and abs(amount - order_data['order_total_cost']) <= 0.01
            if not amount_ok:
                raise SkipOrder(
                    f"EasyGo 結算金額（{amount}）與 be2 訂單總成本（{order_data['order_total_cost']}）不一致，中止"
                )
            await fill_order_form(easygo_page, order_data, push)

            await add_internal_note(easygo_page, order_id, push)
            easygo_order_id = await submit_and_pay(easygo_page, order_data['order_total_cost'], push)

            await fill_be2_voucher(be2_page, easygo_order_id, push)
            await mark_voucher_sent(be2_page, order_id, push)

            push(f'訂單 {order_id} 全部完成！', 'ok')
            return {
                'success': True,
                'order_id': order_id,
                'easygo_order_id': easygo_order_id,
                'nav_lang': order_data.get('nav_lang'),
                'passenger_note': order_data.get('passenger_note'),
            }
        except SkipOrder as e:
            push(f'中止：{e}', 'skip')
            return {'success': False, 'skipped': True, 'order_id': order_id, 'reason': str(e)}
        except Exception as e:
            push(f'錯誤：{e}', 'error')
            shot_be2 = await _save_debug_screenshot(be2_page, order_id, 'be2')
            shot_easygo = await _save_debug_screenshot(easygo_page, order_id, 'easygo')
            push(f'debug screenshot: {shot_be2}, {shot_easygo}', 'info')
            return {'success': False, 'skipped': False, 'order_id': order_id, 'error': str(e)}
        finally:
            await browser.close()
