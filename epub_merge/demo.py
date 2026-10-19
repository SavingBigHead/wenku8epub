"""Synthetic, freely reusable sample files; no real novel content."""
import io
import zipfile

TARGET = '''第一章 雨夜来信
夜里十点，雨水沿着旧书店的玻璃窗滑落。
林夏推开木门，门上的铜铃轻轻响了一声。
柜台上放着一封没有署名的信，信封边缘已经被雨水打湿。
她小心地展开信纸，看见上面画着一盏灯和一条通向海边的小路。
“明天日出以前，到灯塔来。”信的最后只有这一句话。
她收好信，决定先去找住在街角的朋友。
第二章 去往海边
清晨的街道还没有苏醒，远处传来了第一班电车的声音。
朋友已经等在车站，两个人各自提着一盏旧提灯。
海风吹动白色围巾，她们终于看见晨雾里的灯塔。
塔门没有锁，石阶上散落着几片银色的羽毛。
她们沿着旋转的楼梯向上走，脚步声在空荡的塔内回响。
第三章 灯火
灯塔顶层的窗户敞开着，海平线被朝霞染成了淡红色。
桌上放着一本旧相册，第一页就是那间熟悉的书店。
林夏把相册抱在怀里，终于读懂了这封信的意思。
＊　＊　＊
日出以后，她们带着相册回到了书店。
'''


def artwork(label, background, accent, mode):
    scene = ('<circle cx="520" cy="260" r="92" fill="%s"/><path d="M0 750 L220 470 L420 740 L600 560 L720 740 V1080 H0Z" fill="#203f3b"/>' % accent
             if mode == 'sea' else '<rect x="175" y="180" width="370" height="500" rx="180" fill="%s"/><path d="M360 180 V680 M175 440 H545" stroke="#e7eddb" stroke-width="10"/><rect x="100" y="730" width="520" height="16" fill="#203f3b"/>' % accent)
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="720" height="1080" viewBox="0 0 720 1080">'
            '<rect width="720" height="1080" fill="%s"/>%s<text x="360" y="900" text-anchor="middle" font-size="36" fill="#203f3b" font-family="sans-serif">%s</text>'
            '<text x="360" y="960" text-anchor="middle" font-size="20" fill="#45615a" font-family="sans-serif">本地合并原型 · 合成示例</text></svg>') % (background, scene, label)


def demo_files():
    first, second, third = TARGET.strip().split('第二章 去往海边\n')[0], '', ''
    lines = TARGET.strip().splitlines()
    ch1 = lines[1:7]
    ch2 = lines[8:13]
    ch3 = lines[14:]
    def paragraphs(items):
        return ''.join('<p>' + item + '</p>' for item in items)
    def page(content):
        return '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>示例</title></head><body>' + content + '</body></html>'
    entries = {
        'cover.xhtml': page('<img src="../Images/cover.svg"/>'),
        'front.xhtml': page('<img src="../Images/front.svg"/>'),
        'ch1a.xhtml': page('<h1>第一章 雨夜来信</h1>' + paragraphs(ch1[:3])),
        'plate.xhtml': page('<svg xmlns="http://www.w3.org/2000/svg"><image href="../Images/letter.svg"/></svg>'),
        'ch1b.xhtml': page(paragraphs(ch1[3:])),
        'ch2.xhtml': page('<h1>第二章 去往海边</h1>' + paragraphs(ch2[:3]) + '<p><img src="../Images/coast.svg"/></p>' + paragraphs(ch2[3:])),
        'ch3.xhtml': page('<h1>第三章 灯火</h1>' + paragraphs(ch3)),
    }
    manifest = ''.join('<item id="d%d" href="Text/%s" media-type="application/xhtml+xml"/>' % (i, name) for i, name in enumerate(entries))
    images = {
        'cover.svg': artwork('雨夜来信 · 示例封面', '#f2e9d9', '#d7a969', 'window'),
        'front.svg': artwork('卷首彩页 · 海边小镇', '#e4edf1', '#b7cdd7', 'sea'),
        'letter.svg': artwork('正文插图 · 那封来信', '#f2e9d9', '#b9cbb9', 'window'),
        'coast.svg': artwork('正文插图 · 晨雾中的海', '#e4edf1', '#d9b988', 'sea'),
    }
    manifest += ''.join('<item id="a%d" href="Images/%s" media-type="image/svg+xml"%s/>' % (i, name, ' properties="cover-image"' if i == 0 else '') for i, name in enumerate(images))
    opf = ('<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">'
           '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="uid">demo</dc:identifier><dc:title>雨夜来信 · 插图源示例</dc:title><dc:language>zh-CN</dc:language></metadata>'
           '<manifest>%s</manifest><spine>%s</spine></package>') % (manifest, ''.join('<itemref idref="d%d"/>' % i for i in range(len(entries))))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
        z.writestr('META-INF/container.xml', '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>')
        z.writestr('OEBPS/content.opf', opf)
        # Deliberately reverse ZIP storage order: readers must obey the spine.
        for name, content in reversed(list(entries.items())):
            z.writestr('OEBPS/Text/' + name, content)
        for name, content in images.items():
            z.writestr('OEBPS/Images/' + name, content)
    return TARGET.encode('utf-8'), buf.getvalue()


def mojimoon_demo_files():
    """Exercise the actual vendored upstream maker, offline with synthetic text."""
    from pathlib import Path
    import tempfile
    from third_party.mojimoon_wenku8 import epub_maker as maker
    from .books import parse_text
    target, donor = demo_files()
    parsed = parse_text(target, 'sample.txt')
    chapters = [maker.Chapter(c.title, [('p', p.text) for p in c.paragraphs]) for c in parsed.chapters]
    metadata = maker.NovelMeta(title='雨夜来信 第01卷', author='合成示例', description='这是本地合并工具的合成示例，用于校对流程演示。',
                               source_url='https://www.wenku8.net/book/129.htm', identifier='urn:wenku8:demo:v1', series='雨夜来信', series_index=1)
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / 'v01.epub'
        maker.create_epub(metadata, [maker.Volume('第01卷', chapters)], {}, None, str(path), prefix='v01_')
        return path.read_bytes(), donor
