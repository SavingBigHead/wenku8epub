## 自行编辑epub指南

建议用 **Sigil** 打开 EPUB，先另存一份副本，然后修改样式表、正文和元数据。只调整排版时，通常改样式表就够了。Calibre 的“编辑书籍”也可以编辑这些文件。

这些书的章节文件保留原位置，避免破坏脚注和图片链接；新增样式、简介和目录使用统一位置。

| 用途       | 窄门                     | 毛主席语录                  | 文化苦旅                   |
| -------- | ---------------------- | ---------------------- | ---------------------- |
| 统一样式表    | OEBPS/Styles/style.css | OEBPS/Styles/style.css | OEBPS/Styles/style.css |
| 简介页      | OEBPS/Text/Intro.xhtml | OEBPS/Text/Intro.xhtml | OEBPS/Text/Intro.xhtml |
| EPUB3 目录 | OEBPS/nav.xhtml        | OEBPS/nav.xhtml        | OEBPS/nav.xhtml        |
| 兼容目录     | OEBPS/toc.ncx          | OEBPS/toc.ncx          | OEBPS/toc.ncx          |
| 书籍信息文件   | content.opf            | content.opf            | OEBPS/content.opf      |
| 正文示例     | text/part0004.html     | chapter01.html         | OEBPS/text00005.html   |
| 封面页      | titlepage.xhtml        | titlepage.xhtml        | OEBPS/Text/Cover.xhtml |
| 封面图片     | images/00001.jpeg      | cover.png              | OEBPS/Image00004.jpg   |

上表中的路径是 EPUB 内部路径，在 Sigil 的文件列表中查找即可。

### 改正文间距、缩进和标题

打开统一样式表，修改现有规则。例如：

```css
body {
  margin: 0 1%;
  padding: 0;
  line-height: 1.5;
}
p {
  margin: 0;
  text-indent: 2em;
  text-align: justify;
}
h1 {
  line-height: 1.3;
  text-align: center;
  font-weight: bold;
  font-size: 1.4em;
  margin: 1em 0;
}
```

行距由 line-height 控制；首行缩进由 text-indent 控制。想加少量段间距，可把 p 的 margin 改为 0.3em 0；标题字号改 font-size。em 是相对字号的单位，会随阅读字号缩放。

修改 p 会影响普通正文；简介、出处、落款等有单独规则，需要分别改。若阅读器开启了强制统一排版，需要允许使用书籍样式，才能看到这些改动。

### 改特殊段落

本次把原来难懂的 calibre 类名整理成以下用途：

| 样式名                                | 用途           | 修改位置                          |
| ---------------------------------- | ------------ | ----------------------------- |
| p.numerals                         | 居中的小节数字      | 字号、加粗、上下留白                    |
| p.center                           | 题辞等居中段落      | 居中和上下留白                       |
| p.meta                             | 简介页作者、出版社    | 对齐和字号                         |
| p.signature                        | 作者落款、日期      | 右对齐和间距                        |
| p.provenance                       | 语录后面的出处      | 字号、边距、对齐                      |
| blockquote.letter                  | 窄门中的书信和引用    | 左右留白；内部段落保留正文大小               |
| section.footnotes / aside.footnote | 章末脚注区和单条脚注   | 分隔线、字号、段间距                    |
| div.illust / p.caption             | 插图和图片说明      | 图片宽度、说明文字样式                   |
| span.quote-text                    | 原来用楷体区分的行内引文 | 目前使用斜体；可改为 font-style: normal |
| img.rare-glyph                     | 文化苦旅中的生僻字字形图 | 保持行内显示及 0.8em 宽度              |

例如，把语录出处改为左对齐，修改 p.provenance 的 text-align: right 为 left；不需要改动出处文字。

### 改章节标题和目录

章节的标题文字位于正文文件的 h1 中。修改时保留已有 id：

```html
<h1 id="原来的编号">新的章节标题</h1>
```

页面 head 中的 title 也应相应修改。然后用 Sigil 的目录生成功能更新目录，并检查封面、简介、题辞等前置项目是否仍然存在。保持一级目录即可。

如果手动改目录，需要同步修改 OEBPS/nav.xhtml 中的链接文字和 OEBPS/toc.ncx 中的 navLabel/text；href、src 和 # 后面的锚点编号应继续对应正文。

### 改作者、简介、标签

在 Sigil 的元数据编辑器里修改通常最方便。它们存放在上表所列的 OPF 文件中：

```xml
<dc:title>窄门</dc:title>
<dc:creator id="meta_creator">安德烈·纪德</dc:creator>
<dc:description>这里填写书库软件读取的简介。</dc:description>
<dc:subject>法国文学</dc:subject>
<dc:subject>小说</dc:subject>
```

这是已有字段的简化示例：编辑现有值，不要重复粘贴字段；原有作者 id 和相关 meta 属性应保留。每个标签一个 dc:subject。

**元数据简介与可阅读简介页是两个地方。** 修改 OPF 的 dc:description 后，还应修改 OEBPS/Text/Intro.xhtml 中 div.intro 的内容，才能让两边一致。书名、作者、出版社也一样：书库显示读取元数据，阅读页面显示来自简介页。

用 Calibre 主界面修改元数据时，修改可能先保存在书库数据库里；导出前使用相应的嵌入元数据或更新 EPUB 功能，再确认导出的文件已包含改动。

### 换图、插图和脚注

替换已有图片时，优先使用编辑器的替换功能，保持原有引用正确。文件后缀要与真实图片格式相符。添加新图片时，让编辑器把图片纳入 EPUB 资源清单，再插到正文中需要的位置。

正文插图采用下面的结构；src 路径必须相对于当前章节文件，不能原样套用其他章节的路径：

```html
<div class="illust">
  <img src="相对于当前章节的图片路径.jpg" alt=""/>
</div>
```

同一处多张图片，按代码中 div 的前后顺序阅读。要放在本章开头，就放在本章标题后的正文前；要放在上一章结尾，就放进上一章文件的最后。这两种位置在分页和章节跳转时不同。

《文化苦旅》的 img.rare-glyph 是正文中的字形，不要把它当整页插图放大或另起一页。《窄门》的脚注依赖正文 noteref 与脚注 footnote 的 id/href 配对；改文字可以，删除或重命名编号时需要同时修改对应链接和返回链接。

### 把卷首图集中到独立的“插图”章节

把多张卷首图依次放在同一个 XHTML 文件中，并把这个文件加入阅读顺序和两套目录。它是普通的阅读章节，位于“简介”之后、前言或第一章之前；它与封面页分开，正文中不必显示“插图”大标题。

#### 第一步：新建或整理插图章节

在 Sigil 中添加一个空白 XHTML 文件，放到需要的位置。如果文件已经存在，直接编辑即可，不要重复添加。

以下是 OEBPS/Text/Section0001.xhtml 的完整示例。图片仍保留在 EPUB 根目录，因此 src 要向上两层：

```html
<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml"
      xmlns:epub="http://www.idpf.org/2007/ops"
      xml:lang="zh-CN" lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <title>插图</title>
  <link href="../Styles/style.css" rel="stylesheet" type="text/css"/>
</head>
<body>
  <div class="illust" id="em_i0002">
    <img src="../../pic1.jpg" alt="插图"/>
  </div>
  <div class="illust" id="em_i0003">
    <img src="../../pic2.jpg" alt="插图"/>
  </div>
</body>
</html>
```

多个 div 的先后顺序就是图片的顺序。这个示例和参考书一样，正文里只有图片，没有可见的章节标题；head 中的 title 是页面名称，不能代替目录项。

“一个章节”也不等于“所有图片挤在一张屏幕或一页里”：它表示图片集中存放在同一个正文文件，阅读器仍会按图片大小、屏幕和阅读模式分页。

如果图片确实放在 OEBPS/Images/ 中，才应写 ../Images/图片名.jpg。图片是否存在和实际存放位置，要以编辑器的资源列表为准。

#### 第二步：检查资源清单和阅读顺序

在 Sigil 中添加文件或调整正文文件顺序，一般会自动维护 OPF。只移动目录项不会自动改变阅读顺序；在阅读文件列表中把“插图”放在简介后、前言前，并检查 OPF 中的 spine 是否一致。

这次 content.opf 中对应的资源清单条目已正确，保留原条目即可：

```xml
<item id="Section0001.xhtml"
      href="OEBPS/Text/Section0001.xhtml"
      media-type="application/xhtml+xml"/>
```

两张图片也必须各自列在 manifest 中；本书已经有这些图片条目，无需重复增加。

spine 中的相关部分如下；这里的 idref 引用上面 item 的 id，不是文件路径：

```xml
<spine toc="em_ncx">
  <itemref idref="titlepage"/>
  <itemref idref="em_intro"/>
  <itemref idref="Section0001.xhtml"/>
  <itemref idref="id1"/>
  <!-- 后面继续保留原有各章 -->
</spine>
```

这是本书相关部分的示例，不要整段替换掉后面的章节。封面仍然是封面；卷首插图不需要增加 cover-image 标记，也不需要设置为第二张封面。

#### 第三步：同步两套目录

在 OEBPS/nav.xhtml 的 toc 导航列表中，在简介后、前言前加入或修正一级目录项：

```html
<li><a href="Text/Section0001.xhtml">插图</a></li>
```

在 OEBPS/toc.ncx 的 navMap 中，将已经删除的旧页面条目换成新插图条目，并按阅读顺序重新排列 id 和 playOrder：

```xml
<navPoint id="navpoint_3" playOrder="3">
  <navLabel><text>插图</text></navLabel>
  <content src="Text/Section0001.xhtml"/>
</navPoint>
```

本书前两个目录项为封面、简介，所以插图是第 3 项，前言改为第 4 项，后续章节依次排列。保留所有仍然存在的正文目录项。

可以使用 Sigil 的目录编辑功能完成这些修改，并检查生成结果。**纯图片章节没有 h1；只按标题重新生成目录时，它可能不会被自动列出，需要手动补上“插图”条目，并确认两套目录都已更新。**

#### 相对路径要从谁的位置开始算

下面全部指向同一个插图章节或同一组图片，但起点不同：

| 写在哪里                                   | 相对路径从哪里开始算     | 本书正确示例                        |
| -------------------------------------- | -------------- | ----------------------------- |
| 根目录 content.opf 的 manifest/href        | OPF 所在目录       | OEBPS/Text/Section0001.xhtml  |
| OEBPS/nav.xhtml 的 a/href               | nav.xhtml 所在目录 | Text/Section0001.xhtml        |
| OEBPS/toc.ncx 的 content/src            | toc.ncx 所在目录   | Text/Section0001.xhtml        |
| OEBPS/Text/Section0001.xhtml 的 img/src | 插图章节所在目录       | ../../pic1.jpg、../../pic2.jpg |
| 同一插图章节的样式表 link/href                   | 插图章节所在目录       | ../Styles/style.css           |

不要因为图片显示在编辑器的“图片”分类里，就假定它实际位于 Images 文件夹。分类名称和 EPUB 包内路径可能不同。

#### 最后确认

保存后重新打开导出的 EPUB，从目录点击“插图”，应能看到两张图；从简介继续翻页也应依次到插图、前言。用 EPUB 检查功能确认不存在图片路径错误、目录目标不存在或资源未登记的问题。

如果阅读器仍显示旧目录，先关闭旧书，再重新导入修正版；有些阅读器会缓存目录和阅读进度。本次修正版文件名为“毛主席语录（卷首插图修正版）.epub”，便于区分。

## 保存后检查什么

1. 打开封面、简介、一个普通章节和一处插图，确认样式、图片比例和分页正常。
2. 点击目录，确认能跳到对应章节；脚注和返回链接也各点一次。
3. 查看书库详情，确认作者、简介、标签能读取。
4. 使用编辑器的 EPUB 检查功能；新增或删除章节、图片时尤其检查资源清单和阅读顺序。
