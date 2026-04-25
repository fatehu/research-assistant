const fs = require("fs");
const path = require("path");
const { execFileSync } = require("child_process");
const {
  AlignmentType,
  BorderStyle,
  Document,
  ExternalHyperlink,
  Footer,
  FootnoteReferenceRun,
  Header,
  HeadingLevel,
  ImageRun,
  LevelFormat,
  PageBreak,
  PageNumber,
  PageOrientation,
  Packer,
  Paragraph,
  SectionType,
  ShadingType,
  Table,
  TableCell,
  TableOfContents,
  TableRow,
  TextRun,
  WidthType,
} = require("docx");

const outDir = process.argv[2] || "/app/uploads/docx_skill_demo";
fs.mkdirSync(outDir, { recursive: true });

const docxPath = path.join(outDir, "research_platform_docx_skill_demo.docx");
const chartPath = path.join(outDir, "platform-capability-roadmap.png");

function makeChart() {
  const py = String.raw`
from PIL import Image, ImageDraw, ImageFont
img = Image.new("RGB", (1200, 620), "white")
draw = ImageDraw.Draw(img)
colors = {
    "ink": (18, 35, 51),
    "muted": (105, 119, 133),
    "grid": (225, 232, 238),
    "blue": (38, 111, 213),
    "green": (21, 151, 120),
    "orange": (234, 127, 36),
    "panel": (244, 248, 251),
}
draw.rectangle((0, 0, 1200, 620), fill=(255, 255, 255))
draw.rectangle((45, 45, 1155, 575), fill=colors["panel"], outline=(205, 216, 226), width=2)
draw.text((70, 68), "Research Platform Capability Growth", fill=colors["ink"])
draw.text((70, 94), "12-month operating model: data, AI, collaboration", fill=colors["muted"])
left, top, right, bottom = 105, 150, 1100, 500
for i in range(6):
    y = top + i * (bottom - top) // 5
    draw.line((left, y, right, y), fill=colors["grid"], width=1)
    draw.text((55, y - 8), f"{100 - i * 20}%", fill=colors["muted"])
for i in range(13):
    x = left + i * (right - left) // 12
    draw.line((x, top, x, bottom), fill=colors["grid"], width=1)
    if i % 3 == 0:
        draw.text((x - 18, bottom + 18), f"M{i}", fill=colors["muted"])
draw.line((left, bottom, right, bottom), fill=colors["ink"], width=2)
draw.line((left, top, left, bottom), fill=colors["ink"], width=2)
series = [
    ("Data readiness", colors["blue"], [18, 26, 37, 45, 55, 63, 68, 73, 78, 84, 88, 92, 95]),
    ("AI workflow maturity", colors["green"], [8, 14, 22, 31, 42, 52, 61, 68, 75, 82, 88, 93, 97]),
    ("Collaboration reuse", colors["orange"], [12, 18, 25, 33, 40, 48, 57, 66, 73, 79, 85, 89, 94]),
]
for name, color, values in series:
    pts = []
    for i, value in enumerate(values):
        x = left + i * (right - left) / 12
        y = bottom - value * (bottom - top) / 100
        pts.append((x, y))
    for a, b in zip(pts, pts[1:]):
        draw.line((a[0], a[1], b[0], b[1]), fill=color, width=5)
    for x, y in pts:
        draw.ellipse((x - 5, y - 5, x + 5, y + 5), fill=color, outline="white", width=2)
legend_x = 760
for idx, (name, color, _) in enumerate(series):
    y = 70 + idx * 28
    draw.rounded_rectangle((legend_x, y, legend_x + 22, y + 14), radius=4, fill=color)
    draw.text((legend_x + 32, y - 2), name, fill=colors["ink"])
img.save(r"${chartPath}")
`;
  execFileSync("python", ["-c", py], { stdio: "inherit" });
}

function text(text, options = {}) {
  return new TextRun({
    text,
    font: options.font || "Microsoft YaHei",
    size: options.size,
    bold: options.bold,
    italics: options.italics,
    color: options.color,
    break: options.break,
  });
}

function para(children, options = {}) {
  return new Paragraph({
    children: Array.isArray(children) ? children : [text(children)],
    heading: options.heading,
    alignment: options.alignment,
    spacing: options.spacing || { after: 160, line: 300 },
    pageBreakBefore: options.pageBreakBefore,
    numbering: options.numbering,
    indent: options.indent,
    border: options.border,
    tabStops: options.tabStops,
  });
}

function pageBreak() {
  return new Paragraph({ children: [new PageBreak()] });
}

function makeCell(content, width, options = {}) {
  const children = Array.isArray(content)
    ? content
    : [para(String(content), { spacing: { after: 40, line: 260 } })];
  return new TableCell({
    width: { size: width, type: WidthType.DXA },
    margins: { top: 100, bottom: 100, left: 120, right: 120 },
    shading: options.fill ? { fill: options.fill, type: ShadingType.CLEAR } : undefined,
    borders: {
      top: { style: BorderStyle.SINGLE, size: 1, color: options.border || "CAD6E2" },
      bottom: { style: BorderStyle.SINGLE, size: 1, color: options.border || "CAD6E2" },
      left: { style: BorderStyle.SINGLE, size: 1, color: options.border || "CAD6E2" },
      right: { style: BorderStyle.SINGLE, size: 1, color: options.border || "CAD6E2" },
    },
    children,
  });
}

function makeTable(headers, rows, widths, options = {}) {
  return new Table({
    width: { size: widths.reduce((a, b) => a + b, 0), type: WidthType.DXA },
    columnWidths: widths,
    rows: [
      new TableRow({
        tableHeader: true,
        children: headers.map((header, idx) =>
          makeCell([para([text(header, { bold: true, color: "FFFFFF" })], { alignment: AlignmentType.CENTER })], widths[idx], {
            fill: options.headerFill || "123B5D",
            border: "123B5D",
          }),
        ),
      }),
      ...rows.map((row) =>
        new TableRow({
          children: row.map((value, idx) => makeCell(String(value), widths[idx], { fill: idx === 0 ? "F2F6FA" : undefined })),
        }),
      ),
    ],
  });
}

function footer() {
  return new Footer({
    children: [
      new Paragraph({
        alignment: AlignmentType.RIGHT,
        children: [
          text("科研平台建设方案 | 第 ", { size: 18, color: "5D6D7E" }),
          new TextRun({ children: [PageNumber.CURRENT], size: 18, color: "5D6D7E" }),
          text(" 页", { size: 18, color: "5D6D7E" }),
        ],
        border: { top: { color: "D7E1EA", space: 1, style: BorderStyle.SINGLE, size: 4 } },
      }),
    ],
  });
}

function header() {
  return new Header({
    children: [
      new Paragraph({
        children: [text("Research Platform Proposal", { bold: true, color: "123B5D", size: 18 })],
        alignment: AlignmentType.RIGHT,
      }),
    ],
  });
}

makeChart();

const contentWidth = 9360;
const sections = [];

const coverChildren = [
  para(""),
  para(""),
  para([text("科研平台建设方案", { bold: true, size: 56, color: "123B5D" })], { alignment: AlignmentType.CENTER, spacing: { after: 180 } }),
  para([text("Research Platform Construction Proposal", { bold: true, size: 28, color: "2A6F97" })], {
    alignment: AlignmentType.CENTER,
    spacing: { after: 640 },
  }),
  para([text("面向论文阅读、知识管理、智能复现与科研项目协作的一体化平台模板", { size: 24, color: "31485E" })], {
    alignment: AlignmentType.CENTER,
    spacing: { after: 560 },
  }),
  makeTable(
    ["字段", "内容"],
    [
      ["适用场景", "科研平台立项、国基申报支撑、团队基础设施建设"],
      ["文档版本", "v1.0 / 复杂 DOCX 能力测试稿"],
      ["编制日期", "2026-04-24"],
      ["生成链路", "Claude Code document-skills:docx + docx-js + LibreOffice render check"],
    ],
    [2200, 7160],
  ),
  para(""),
  para([text("保密级别：内部评审 | 生成后请在 Word 中更新目录页码", { color: "8A4B08", size: 20 })], {
    alignment: AlignmentType.CENTER,
    spacing: { before: 520 },
  }),
  pageBreak(),
  para([text("目录", { bold: true, size: 32, color: "123B5D" })], { alignment: AlignmentType.CENTER }),
  new TableOfContents("Table of Contents", { hyperlink: true, headingStyleRange: "1-4" }),
  para([text("提示：Word 打开后右键目录并选择“更新域”可刷新页码。", { size: 18, color: "5D6D7E" })], { spacing: { before: 360 } }),
  pageBreak(),
];

const body = [
  para("一、执行摘要", { heading: HeadingLevel.HEADING_1 }),
  para("本方案用于验证复杂 DOCX 自动生成能力，同时展示科研平台正式方案应具备的结构。平台目标是把论文阅读、知识库管理、实验复现、智能调优和项目文档生成整合为统一工作流。"),
  para([
    text("关键判断：", { bold: true, color: "123B5D" }),
    text(" 文档生成能力不能停留在“可打开”，还必须具备稳定版式、结构化目录、复杂表格、图片、脚注、横向页面和后续可编辑性。"),
    new FootnoteReferenceRun(1),
  ]),
  para([text("提示：附录 A 提供术语表，便于评审专家快速理解平台工程概念。", { color: "2A6F97" })]),
  para([text("一键入口：", { bold: true }), new ExternalHyperlink({ link: "https://agentskills.io/specification", children: [text("Agent Skills Specification", { color: "2A6F97" })] })]),

  para("二、研究背景", { heading: HeadingLevel.HEADING_1 }),
  para("2.1 科研平台建设的主要矛盾", { heading: HeadingLevel.HEADING_2 }),
  para("大量科研团队已经拥有论文、数据、代码、实验记录和报告模板，但这些资源分散在不同系统里，导致复现链路断裂、知识沉淀薄弱、项目交付依赖个人经验。"),
  para("2.2 平台能力边界", { heading: HeadingLevel.HEADING_2 }),
  makeTable(
    ["模块", "核心能力", "当前痛点", "优化方向"],
    [
      ["论文阅读", "PDF 解析、摘要、图表理解", "上下文割裂，证据定位弱", "以段落、图表、引用链组织证据"],
      ["文献管理", "检索、收藏、入库、去重", "只绑定单一知识库", "一篇论文可进入多个集合，查询时按可用库选择"],
      ["实验复现", "代码仓库、环境、日志", "半克隆/超时导致误判", "准备阶段引入完整性校验和重试"],
      ["文档生成", "国基、科研项目、教研项目模板", "模板与输出难以稳定复用", "模板 skill 化，内容策划与 docx 生成分离"],
    ],
    [1800, 2500, 2500, 2560],
  ),

  para("三、总体架构", { heading: HeadingLevel.HEADING_1 }),
  para("3.1 分层架构", { heading: HeadingLevel.HEADING_2 }),
  para("平台采用资源层、数据层、智能层、协作层和治理层五层结构，确保工具链可独立演进，同时通过统一任务编排串联端到端科研流程。"),
  makeTable(
    ["层级", "责任", "典型组件"],
    [
      ["资源层", "提供算力、存储、容器运行环境", "Docker、GPU Worker、对象存储"],
      ["数据层", "管理论文、PDF、摘要、向量、元数据", "PostgreSQL、向量索引、文献 API"],
      ["智能层", "执行阅读、分类、复现策划、调优决策", "LLM Agent、Skill、工具调用"],
      ["协作层", "支持会话分支、模板共创、交付审阅", "Chat Session、Branch、Doc Template"],
      ["治理层", "控制权限、审计、队列、运行状态", "Job Queue、Audit Log、Readiness Check"],
    ],
    [1800, 3900, 3660],
  ),
  para("3.2 能力增长图", { heading: HeadingLevel.HEADING_2 }),
  para("下图展示平台在 12 个月内的数据准备度、AI 工作流成熟度和协作复用率的预期变化。"),
  new Paragraph({
    alignment: AlignmentType.CENTER,
    children: [
      new ImageRun({
        type: "png",
        data: fs.readFileSync(chartPath),
        transformation: { width: 560, height: 289 },
        altText: {
          title: "Research platform capability growth chart",
          description: "Line chart showing data readiness, AI workflow maturity, and collaboration reuse growth over 12 months.",
          name: "platform-capability-roadmap",
        },
      }),
    ],
  }),
  para("图 1：平台能力增长曲线。", { alignment: AlignmentType.CENTER }),

  para("四、AI 工作流程", { heading: HeadingLevel.HEADING_1 }),
  para("4.1 策划者与执行者分工", { heading: HeadingLevel.HEADING_2 }),
  para("Agent 在科研平台中优先作为策划者：负责论文解读、复现准备、风险判断、实验设计和调优方向；具体代码修改、环境执行和交付由 Claude Code 或专用 worker 执行。"),
  para("4.1.1 Skill 激活后的常驻提示", { heading: HeadingLevel.HEADING_3 }),
  para("当用户启用论文复现、文档共创等 skill 后，平台应把对应职责边界注入 session 级 system prompt，而不是临时写入单轮 prompt。"),
  para("4.1.1.1 工具失败时的边界提醒", { heading: HeadingLevel.HEADING_4 }),
  para("当模型调用工具失败时，应提示工具适用范围和替代路径，禁止模型自行切换到不符合设计的执行角色。"),
  para("4.2 工作流步骤", { heading: HeadingLevel.HEADING_2 }),
  para("需求澄清", { numbering: { reference: "steps", level: 0 } }),
  para("资源准备与 readiness 检查", { numbering: { reference: "steps", level: 0 } }),
  para("证据检索与结构化解读", { numbering: { reference: "steps", level: 0 } }),
  para("任务拆解与 worker 执行", { numbering: { reference: "steps", level: 0 } }),
  para("结果审阅、分支和文档交付", { numbering: { reference: "steps", level: 0 } }),

  para("五、数据治理与队列机制", { heading: HeadingLevel.HEADING_1 }),
  para("PDF 入库、摘要抓取、向量化和图表理解都应进入队列执行，避免同步请求卡死页面。队列任务必须具备状态、重试、超时、取消和失败原因。"),
  makeTable(
    ["任务类型", "是否排队", "推荐超时", "失败处理"],
    [
      ["PDF 解析", "是", "10-20 分钟", "保留原文件，提示解析阶段"],
      ["摘要抓取", "是", "2-5 分钟", "记录 API 和请求参数"],
      ["向量入库", "是", "5-15 分钟", "按 chunk 重试"],
      ["文档生成", "是", "5-10 分钟", "保留中间 JS / 渲染日志"],
    ],
    [1900, 1500, 1800, 4160],
  ),

  para("六、风险矩阵", { heading: HeadingLevel.HEADING_1 }),
  makeTable(
    ["风险", "概率", "影响", "等级", "缓解动作"],
    [
      ["第三方文献 API 限流", "中", "高", "橙色", "缓存摘要、分批抓取、保留 provider 元数据"],
      ["LLM 工具误用", "中", "高", "橙色", "系统提示常驻化、工具失败边界提示"],
      ["Docker build 网络波动", "高", "中", "黄色", "apt/npm 换源，插件运行态安装"],
      ["DOCX 版式不可控", "中", "中", "黄色", "docx-js 生成，LibreOffice 渲染复核"],
      ["半克隆仓库被误判 ready", "低", "高", "橙色", "检查 HEAD、工作树文件和 README intake"],
    ],
    [2500, 1100, 1100, 1200, 3460],
    { headerFill: "7A271A" },
  ),

  para("七、12 个月路线图", { heading: HeadingLevel.HEADING_1 }),
  makeTable(
    ["月份", "里程碑", "交付物", "验收标准"],
    [
      ["1-2", "文献检索与入库稳定化", "分页检索、去重、收藏夹 readiness", "搜索参数和 API 行为一致"],
      ["3-4", "PDF 处理队列化", "任务状态、重试、取消", "页面不因解析任务卡死"],
      ["5-6", "Agent 工具边界治理", "失败提示、职责常驻 system prompt", "模型不自行承担 worker 职责"],
      ["7-8", "科研文档模板系统", "国基、项目、教研模板", "模板可配置、可复用、可迭代"],
      ["9-10", "复现项目闭环", "仓库准备、环境探测、实验计划", "ready 状态可信"],
      ["11-12", "多团队试点", "使用报告、质量评估", "用户满意度 >= 4.3/5"],
    ],
    [1200, 2700, 2700, 2760],
  ),

  para("八、预算规划", { heading: HeadingLevel.HEADING_1 }),
  makeTable(
    ["预算项", "金额", "占比", "说明"],
    [
      ["算力与存储", "80 万", "40%", "GPU worker、对象存储、备份"],
      ["文献与 API", "30 万", "15%", "Crossref、OpenAlex、Semantic Scholar 等额度"],
      ["平台开发", "60 万", "30%", "队列、检索、Agent、文档模块"],
      ["测试与运维", "20 万", "10%", "监控、日志、灾备演练"],
      ["培训与试点", "10 万", "5%", "用户培训和团队试点"],
    ],
    [2200, 1600, 1300, 4260],
  ),

  para("九、评价指标", { heading: HeadingLevel.HEADING_1 }),
  makeTable(
    ["指标", "基线", "目标", "采集方式"],
    [
      ["论文入库成功率", "未知", ">= 98%", "任务日志 + 文件校验"],
      ["重复入库处理时间", "人工判断", "< 2 秒", "元数据哈希和 DOI 去重"],
      ["AI 回答证据命中率", "不足", ">= 85%", "引用段落和页面校验"],
      ["文档生成可打开率", "不稳定", "100%", "validate.py + LibreOffice 转 PDF"],
      ["用户流程中断率", "高", "< 5%", "前端埋点 + 后端任务状态"],
    ],
    [2600, 1700, 1700, 3360],
  ),

  para("十、参考文献", { heading: HeadingLevel.HEADING_1 }),
  para("OpenAlex API Documentation. https://docs.openalex.org/"),
  para("Crossref REST API Documentation. https://api.crossref.org/"),
  para("Semantic Scholar Recommendations and Graph API Documentation."),
  para("Agent Skills Specification. https://agentskills.io/specification"),
  para("Microsoft Office Open XML ECMA-376 Standard."),

  para("十一、附录", { heading: HeadingLevel.HEADING_1 }),
  para("附录 A：术语表", { heading: HeadingLevel.HEADING_2 }),
  makeTable(
    ["术语", "定义"],
    [
      ["Readiness", "资源是否具备进入下一步处理的状态判断"],
      ["Skill", "面向特定任务的可复用指令、脚本和资源包"],
      ["Branch", "复制当前 session 内容到新 session 的轻量分支能力"],
      ["Worker", "负责实际执行任务的运行环境或工具，如 Claude Code runtime worker"],
    ],
    [2200, 7160],
  ),
  para("附录 B：生成链路说明", { heading: HeadingLevel.HEADING_2 }),
  para("本样例使用 docx-js 生成 DOCX，使用官方 document-skills:docx 规则约束结构，并使用 LibreOffice 转 PDF、pdftoppm 渲染 PNG 进行基本版式检查。"),
];

sections.push({
  properties: {
    page: {
      size: { width: 12240, height: 15840 },
      margin: { top: 1080, right: 1440, bottom: 1080, left: 1440 },
    },
  },
  headers: { default: header() },
  footers: { default: footer() },
  children: [...coverChildren, ...body.slice(0, 22)],
});

sections.push({
  properties: {
    type: SectionType.NEXT_PAGE,
    page: {
      size: { width: 12240, height: 15840, orientation: PageOrientation.LANDSCAPE },
      margin: { top: 720, right: 720, bottom: 720, left: 720 },
    },
  },
  headers: { default: header() },
  footers: { default: footer() },
  children: [
    para("横向附表：平台能力-组件-指标映射", { heading: HeadingLevel.HEADING_1 }),
    makeTable(
      ["能力域", "前端体验", "后端服务", "数据资产", "AI 能力", "运维指标"],
      [
        ["论文检索", "分页加载、排序、筛选 hover 解释", "官方 API 参数透传", "DOI、摘要、引用数", "查询意图改写", "P95 < 2s"],
        ["PDF 入库", "任务进度可视化", "队列、重试、取消", "页码、段落、图表", "结构化摘要", "成功率 >= 98%"],
        ["复现准备", "项目 readiness 面板", "仓库完整性校验", "README intake", "复现计划生成", "半克隆误判为 0"],
        ["文档生成", "模板编辑器", "docx-js 渲染链路", "模板版本库", "内容策划和修订", "可打开率 100%"],
      ],
      [1900, 2500, 2500, 2500, 2500, 2140],
      { headerFill: "154C79" },
    ),
    pageBreak(),
  ],
});

sections.push({
  properties: {
    type: SectionType.NEXT_PAGE,
    page: {
      size: { width: 12240, height: 15840 },
      margin: { top: 1080, right: 1440, bottom: 1080, left: 1440 },
    },
  },
  headers: { default: header() },
  footers: { default: footer() },
  children: body.slice(22),
});

const doc = new Document({
  creator: "Research Assistant Runtime Worker",
  title: "科研平台建设方案 - DOCX Skill Demo",
  description: "Complex DOCX generated with docx-js following document-skills rules.",
  styles: {
    default: {
      document: { run: { font: "Microsoft YaHei", size: 22 } },
    },
    paragraphStyles: [
      {
        id: "Heading1",
        name: "Heading 1",
        basedOn: "Normal",
        next: "Normal",
        quickFormat: true,
        run: { size: 32, bold: true, color: "123B5D", font: "Microsoft YaHei" },
        paragraph: { spacing: { before: 280, after: 180 }, outlineLevel: 0 },
      },
      {
        id: "Heading2",
        name: "Heading 2",
        basedOn: "Normal",
        next: "Normal",
        quickFormat: true,
        run: { size: 26, bold: true, color: "2A6F97", font: "Microsoft YaHei" },
        paragraph: { spacing: { before: 220, after: 140 }, outlineLevel: 1 },
      },
      {
        id: "Heading3",
        name: "Heading 3",
        basedOn: "Normal",
        next: "Normal",
        quickFormat: true,
        run: { size: 23, bold: true, color: "31485E", font: "Microsoft YaHei" },
        paragraph: { spacing: { before: 160, after: 100 }, outlineLevel: 2 },
      },
      {
        id: "Heading4",
        name: "Heading 4",
        basedOn: "Normal",
        next: "Normal",
        quickFormat: true,
        run: { size: 21, bold: true, italics: true, color: "5D6D7E", font: "Microsoft YaHei" },
        paragraph: { spacing: { before: 120, after: 80 }, outlineLevel: 3 },
      },
    ],
  },
  numbering: {
    config: [
      {
        reference: "steps",
        levels: [
          {
            level: 0,
            format: LevelFormat.DECIMAL,
            text: "%1.",
            alignment: AlignmentType.LEFT,
            style: { paragraph: { indent: { left: 720, hanging: 360 } } },
          },
        ],
      },
      {
        reference: "bullets",
        levels: [
          {
            level: 0,
            format: LevelFormat.BULLET,
            text: "•",
            alignment: AlignmentType.LEFT,
            style: { paragraph: { indent: { left: 720, hanging: 360 } } },
          },
        ],
      },
    ],
  },
  footnotes: {
    1: {
      children: [
        para("本脚注用于验证 docx-js footnotes 能力；真实项目中可放置数据来源、方法说明或审计结论。", {
          spacing: { after: 80, line: 240 },
        }),
      ],
    },
  },
  sections,
});

Packer.toBuffer(doc).then((buffer) => {
  fs.writeFileSync(docxPath, buffer);
  console.log(docxPath);
});
