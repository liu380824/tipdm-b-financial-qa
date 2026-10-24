import os
import re

import fitz

from config import CONFIG

def main() -> None:
    reports_dir = CONFIG.get('reports_dir', '')
    pdf_path = ''
    for root, _dirs, files in os.walk(reports_dir):
        for name in sorted(files):
            if name.lower().endswith('.pdf'):
                pdf_path = os.path.join(root, name)
                break
        if pdf_path:
            break

    if not pdf_path:
        raise FileNotFoundError('未在 reports_dir 下找到可用于检查的 PDF 文件')

    text = ''
    with fitz.open(pdf_path) as doc:
        for page in doc:
            text += page.get_text() + '\n'

    print(f"总字符数: {len(text)}")

    for anchor in ('合并现金流量表', '现金流量表'):
        idx = text.find(anchor)
        if idx != -1:
            print(f'\n=== 找到"{anchor}"位置: {idx} ===')
            print(repr(text[idx:idx+3000]))
            break

    print('\n=== 正则测试 ===')
    patterns_test = [
        ('经营活动净额-原正则', r'经营活动产生的现金流量净额[^\n]{0,60}?([-\d,]+\.\d+)'),
        ('经营活动净额-新正则', r'经营活动产生的现金[\s\n]*流量净额[\s\S]{0,5}?([-\d,]+\.\d+)'),
        ('净现金流-原正则', r'现金及现金等价物净增加额[^\n]{0,40}?([-\d,]+\.\d+)'),
        ('净现金流-新正则', r'现金及现金等价物净增加额[\s\S]{0,5}?([-\d,]+\.\d+)'),
        ('投资活动-新正则', r'投资活动产生的现金流量净额[\s\S]{0,5}?([-\d,]+\.\d+)'),
        ('筹资活动-新正则', r'筹资活动产生的现金流量净额[\s\S]{0,5}?([-\d,]+\.\d+)'),
        ('销售商品-新正则', r'销售商品[\s\S]{0,20}?提供劳务收到的现金[\s\S]{0,5}?([-\d,]+\.\d+)'),
    ]
    for name, pat in patterns_test:
        m = re.search(pat, text)
        print(f'  {name}: {m.group(1) if m else "未匹配"}')
        if m:
            print(f'    context: {repr(text[m.start():m.end()+10])}')


if __name__ == '__main__':
    main()
