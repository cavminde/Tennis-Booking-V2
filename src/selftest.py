# -*- coding: utf-8 -*-
"""离线自检（不联网）：规划 / 随机 / 判定 / 速率 / 配置。
运行： python selftest.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import booker as b

FAIL = []


def check(cond, msg):
    if cond:
        print(f'  ✓ {msg}')
    else:
        print(f'  ✗ {msg}')
        FAIL.append(msg)


print('[1] 时段枚举（只订 1 小时）')
slots = b.enumerate_slots('06:00', '22:00')
check(len(slots) == 16, f'06:00-22:00 → 16 个整点时段（实际 {len(slots)}）')
check(slots[0] == '06:00-07:00' and slots[-1] == '21:00-22:00',
      f'首尾正确：{slots[0]} ... {slots[-1]}')
check(all(int(s.split('-')[1][:2]) - int(s.split('-')[0][:2]) == 1 for s in slots),
      '每个时段都是整 1 小时')
check(b.enumerate_slots('18:00', '22:00') == ['18:00-19:00', '19:00-20:00',
                                              '20:00-21:00', '21:00-22:00'],
      '18:00-22:00 → 4 段')
check(b.enumerate_slots('18:00', '18:30') == [], '不足 1 小时 → 空')

print('\n[2] 开放表（周一~周四 19-21 是专业课，不开放）')
c4 = {'name': '4号场', 'sku': 'x',
      'weekly': b.FALLBACK_WEEKLY['4号场']}
c1 = {'name': '1号场', 'sku': 'y',
      'weekly': b.FALLBACK_WEEKLY['1号场']}
check(b.court_open_at(c4, '周一', '18:00-19:00'), '周一 4号场 18-19 开放')
check(not b.court_open_at(c4, '周一', '19:00-20:00'), '周一 4号场 19-20 不开放（专业课）')
check(not b.court_open_at(c4, '周一', '20:00-21:00'), '周一 4号场 20-21 不开放')
check(b.court_open_at(c4, '周一', '21:00-22:00'), '周一 4号场 21-22 开放')
check(b.court_open_at(c4, '周五', '20:00-21:00'), '周五 4号场 20-21 开放')
check(not b.court_open_at(c1, '周三', '18:00-19:00'), '周三 1号场 18-19 不开放')
check(b.court_open_at(c1, '周三', '21:00-22:00'), '周三 1号场 21-22 开放')

print('\n[3] 候选生成与排序')
courts = [{'name': f'{i}号场', 'sku': f'sku{i}',
           'weekly': b.FALLBACK_WEEKLY[f'{i}号场']} for i in range(1, 8)]
slots = b.enumerate_slots('18:00', '22:00')
order = ['4号场', '5号场', '6号场', '7号场', '2号场', '3号场', '1号场']
enabled = {c['name']: True for c in courts}
occ = {'sku4': {'18:00-19:00'}, 'sku5': set(), 'sku6': set(), 'sku7': set(),
       'sku2': set(), 'sku3': set(), 'sku1': set()}
tmp = tempfile.mkdtemp()
rng = b.RandomSource(os.path.join(tmp, '.salt'), enabled=True)

plan = b.make_candidates(courts, '周六', slots, occ, order, enabled, rng, b.SORT_COURT)
check(plan[0]['court'] == '4号场', f'严格顺序：首个候选是 4号场（实际 {plan[0]["court"]}）')
check(plan[0]['slot'] == '19:00-20:00', f'4号场 18-19 被占 → 顺延到 19-20（实际 {plan[0]["slot"]}）')
check(all(p['slot'] != '18:00-19:00' or p['court'] != '4号场' for p in plan),
      '已占用时段不出现在候选里')

plan_t = b.make_candidates(courts, '周六', slots, occ, order, enabled, rng, b.SORT_TIME)
check(plan_t[0]['slot'] == '18:00-19:00', f'时间优先：首个是 18-19（实际 {plan_t[0]["slot"]}）')

plan_r = b.make_candidates(courts, '周六', slots, occ, order, enabled, rng, b.SORT_SHUFFLE)
check(len(plan_r) == len(plan), '完全随机：候选数量不变')

enabled2 = dict(enabled); enabled2['4号场'] = False
plan_e = b.make_candidates(courts, '周六', slots, occ, order, enabled2, rng, b.SORT_COURT)
check(all(p['court'] != '4号场' for p in plan_e), '停用场地不进候选')

plan_wd = b.make_candidates(courts, '周一', slots, occ, order, enabled, rng, b.SORT_COURT)
check(all(p['slot'] in ('18:00-19:00', '21:00-22:00') for p in plan_wd),
      '周一候选只剩 18-19 与 21-22')

print('\n[4] 反冲突随机（非时间种子）')
salt_a = os.path.join(tmp, 'sa'); salt_b = os.path.join(tmp, 'sb')
r1 = b.RandomSource(salt_a, enabled=True)
r2 = b.RandomSource(salt_b, enabled=True)
check(r1.salt != r2.salt, '不同盐文件 → 不同盐值')
check(r1.fingerprint != r2.fingerprint, f'不同签名：{r1.fingerprint} vs {r2.fingerprint}')
r3 = b.RandomSource(salt_a, enabled=True)
check(r3.fingerprint == r1.fingerprint, '同一盐 + 同口令 → 签名稳定（可复现）')
r4 = b.RandomSource(salt_a, passphrase='abc', enabled=True)
check(r4.fingerprint != r1.fingerprint, '加口令后签名改变')
seq1 = [r1.randint(0, 10**6) for _ in range(20)]
seq1b = [b.RandomSource(salt_a, enabled=True).randint(0, 10**6) for _ in range(20)]
check(seq1 != seq1b, '同一台机两次运行序列也不同（混入了 os.urandom）')
src = list(range(50))
sh1 = b.RandomSource(salt_a, enabled=True).shuffle(src)
sh2 = b.RandomSource(salt_b, enabled=True).shuffle(src)
check(sh1 != sh2, '不同脚本打乱结果不同')
check(sorted(sh1) == src, '打乱不丢元素')
off = b.RandomSource(salt_a, enabled=False)
check(off.shuffle(src) == src, '关闭反冲突时保持原序')

print('\n[5] 判定')
check(b.judge({'code': 200, 'msg': '下单成功'}) == 'success', '下单成功 → success')
check(b.judge({'code': 401, 'msg': '请求访问：x，认证失败，无法访问系统资源'}) == 'auth_error',
      'code=401 → auth_error')
check(b.judge({'code': 500, 'msg': '日期超过可提前天数'}) == 'not_open', '超出提前天数 → not_open')
check(b.judge({'code': 500, 'msg': '该时段已被预约'}) == 'conflict', '已被预约 → conflict')
check(b.judge({'code': 500, 'msg': '余额不足'}) == 'pay_error', '余额不足 → pay_error')
check(b.judge({'code': 500, 'msg': '无权限访问'}) == 'no_permission', '无权限 → no_permission')
check(b.judge({'code': 500, 'msg': '请求过于频繁'}) == 'rate_limited', '频繁 → rate_limited')
check(b.judge({'code': 500, 'msg': '每人每天只能预订1小时'}) == 'limit_reached',
      '每人每天1小时 → limit_reached')
check(b.judge('boom', status=429) == 'rate_limited', 'HTTP 429 → rate_limited')

print('\n[6] 速率治理')
g = b.RateGovernor(min_interval=0.10, jitter_ms=0)
t0 = time.perf_counter()
for _ in range(4):
    g.wait()
el = time.perf_counter() - t0
check(el >= 0.28, f'4 次请求至少间隔 0.30s（实际 {el:.3f}s）')
check(abs(g.theoretical_qps - 10) < 0.01, f'0.10s 间隔 → 10 次/秒（实际 {g.theoretical_qps:.1f}）')
g2 = b.RateGovernor(min_interval=0.05, jitter_ms=100)
check(8 < g2.theoretical_qps < 11, f'含抖动后理论值合理（{g2.theoretical_qps:.1f}）')

print('\n[7] 配置与矩阵')
cfg = b.default_config()
check(cfg['court_order'][0] == '4号场', f'默认首选 4号场（{cfg["court_order"][0]}）')
check(len(cfg['court_order']) == 7, '7 片场地齐全')
check(all(cfg['court_enabled'][c['name']] for c in b.COURTS_FALLBACK), '默认全部参与')
m = b.build_matrix(courts, '周六', slots, occ)
check(m['4号场']['18:00-19:00'] == 'occupied', '矩阵：4号场 18-19 标为已占')
check(m['5号场']['18:00-19:00'] == 'open', '矩阵：5号场 18-19 可抢')
m2 = b.build_matrix(courts, '周一', slots, occ)
check(m2['4号场']['19:00-20:00'] == 'closed', '矩阵：周一 19-20 不开放')
check('occupied' not in (b.build_matrix(courts, '周一', slots, {})['1号场'].values()),
      '周一 1号场不出现「已占」误判')

print('\n' + '=' * 50)
if FAIL:
    print(f'✗ {len(FAIL)} 项未通过：')
    for f in FAIL:
        print('   -', f)
    sys.exit(1)
print('✓ 全部通过')
