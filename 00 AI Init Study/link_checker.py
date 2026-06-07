#!/usr/bin/env python3
"""
网站链接检测工具
爬取指定网站的所有站内链接，检测是否存在坏链/断链
仅使用 Python 标准库
"""

import urllib.request
import urllib.parse
import urllib.error
from html.parser import HTMLParser
import ssl
import time
import sys
import logging
import csv
import random
from collections import deque

# ============ 配置 ============
BASE_URL = "https://www.baidu.com"
# 坏链报告CSV输出路径
CSV_OUTPUT = "broken_links_report.csv"
# 所有链接及其来源CSV输出路径
ALL_LINKS_OUTPUT = "all_links_report.csv"
# 最大爬取页面数
MAX_PAGES = 500
# 请求超时（秒）
REQUEST_TIMEOUT = 15
# 请求间隔（秒），避免过于频繁
REQUEST_DELAY = 0.5
# 资源文件扩展名（非HTML页面）
SKIP_EXTENSIONS = {
    '.pdf', '.doc', '.docx', '.docm', '.xls', '.xlsx', '.xlsm',
    '.ppt', '.pptx', '.pptm',
    '.zip', '.rar', '.7z', '.gz', '.tar',
    '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.svg', '.ico', '.webp', '.avif', '.heic',
    '.mp3', '.mp4', '.avi', '.mov', '.wmv', '.flv', '.webm', '.ogg',
    '.css', '.js', '.mjs', '.map', '.woff', '.woff2', '.woff3', '.ttf', '.eot',
    '.xml', '.json', '.rss', '.atom',
}
# 需要跳过的URL模式
SKIP_PATTERNS = [
    r'javascript:', r'mailto:', r'tel:', r'#',
]

# 请求头轮换池
USER_AGENTS = [
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0',
]
ACCEPT_LANGUAGES = [
    'zh-CN,zh;q=0.9,en;q=0.8',
    'zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7',
    'en-US,en;q=0.9,zh-CN;q=0.8',
]

# ============ 日志配置 ============
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# ============ SSL 配置 ============
# 警告：禁用证书验证仅适用于测试/UAT环境，生产环境请勿使用
ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE

# 创建支持 Keep-Alive 的 HTTP 处理器
http_handler = urllib.request.HTTPSHandler(context=ssl_ctx)
opener = urllib.request.build_opener(http_handler)
opener.addheaders = [('Connection', 'keep-alive')]
urllib.request.install_opener(opener)


def get_random_headers():
    """生成随机请求头"""
    return {
        'User-Agent': random.choice(USER_AGENTS),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': random.choice(ACCEPT_LANGUAGES),
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
    }


class LinkExtractor(HTMLParser):
    """从HTML中提取链接的解析器"""

    def __init__(self):
        super().__init__()
        self.links = []
        self.reset()

    def reset(self):
        super().reset()
        self.links = []

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == 'a' and 'href' in attrs_dict:
            self.links.append(attrs_dict['href'])
        elif tag == 'area' and 'href' in attrs_dict:
            self.links.append(attrs_dict['href'])
        elif tag == 'link' and 'href' in attrs_dict:
            # 提取所有link标签的链接，包括CSS/ICO等资源
            self.links.append(attrs_dict['href'])
        elif tag == 'img' and 'src' in attrs_dict:
            self.links.append(attrs_dict['src'])
        elif tag == 'script' and 'src' in attrs_dict:
            self.links.append(attrs_dict['src'])
        elif tag == 'source' and 'src' in attrs_dict:
            self.links.append(attrs_dict['src'])
        elif tag == 'iframe' and 'src' in attrs_dict:
            self.links.append(attrs_dict['src'])
        elif tag in ('video', 'audio') and 'src' in attrs_dict:
            self.links.append(attrs_dict['src'])
        elif tag == 'track' and 'src' in attrs_dict:
            self.links.append(attrs_dict['src'])
        elif tag == 'embed' and 'src' in attrs_dict:
            self.links.append(attrs_dict['src'])
        elif tag == 'object' and 'data' in attrs_dict:
            self.links.append(attrs_dict['data'])


def is_internal_link(href):
    """判断链接是否为站内链接"""
    if not href:
        return False
    href = href.strip()
    # 跳过特殊协议
    for pattern in SKIP_PATTERNS:
        if href.lower().startswith(pattern):
            return False
    # 以 / 开头的相对路径
    if href.startswith('/'):
        return True
    # 以本站域名开头的绝对路径
    if href.lower().startswith(BASE_URL.lower()):
        return True
    # 协议相对路径 //www.baidu.com/...
    if href.startswith('//') and 'www.baidu.com' in href.lower():
        return True
    return False


def normalize_url(href):
    """将链接标准化为完整URL，对查询参数排序以避免重复"""
    href = href.strip()
    # 去除锚点
    if '#' in href:
        href = href.split('#')[0]
    # 去除锚点后可能为空字符串（如原href仅为"#"或""）
    if not href:
        return ''
    # 协议相对路径
    if href.startswith('//'):
        href = 'https:' + href
    # 相对路径
    elif href.startswith('/'):
        href = BASE_URL + href

    # 解析URL，对查询参数排序后重新组装
    parsed = urllib.parse.urlparse(href)
    query = urllib.parse.parse_qsl(parsed.query)
    query_sorted = urllib.parse.urlencode(sorted(query))

    # 去除末尾斜杠统一格式（首页除外）
    path = parsed.path
    if path != '/' and path.endswith('/'):
        path = path.rstrip('/')

    # 重新组装URL
    href = urllib.parse.urlunparse((
        parsed.scheme, parsed.netloc, path,
        parsed.params, query_sorted, ''
    ))

    return href


def is_resource_url(url):
    """判断URL是否为资源文件（非HTML页面）"""
    # 检查文件扩展名
    path = urllib.parse.urlparse(url).path.lower()
    for ext in SKIP_EXTENSIONS:
        if path.endswith(ext):
            return True
    return False


def fetch_url(url):
    """获取URL内容，返回 (status_code, html_content)，支持403/503退避重试"""
    headers = get_random_headers()
    max_retries = 3
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT, context=ssl_ctx) as resp:
                status = resp.status
                # 只处理HTML内容
                content_type = resp.headers.get('Content-Type', '')
                if 'text/html' in content_type or 'application/xhtml' in content_type:
                    html = resp.read().decode('utf-8', errors='replace')
                    return status, html
                else:
                    return status, None
        except urllib.error.HTTPError as e:
            # 对 429/403/503 进行指数退避重试
            if e.code in (429, 403, 503) and attempt < max_retries - 1:
                backoff = 2 ** attempt + random.uniform(0, 1)
                logger.warning(f"HTTP {e.code} for {url}, 退避 {backoff:.1f}s 后重试 (第{attempt + 1}次)")
                time.sleep(backoff)
                headers = get_random_headers()  # 轮换请求头
                continue
            logger.warning(f"HTTPError for {url}: {e.code} {e.reason}")
            return e.code, None
        except urllib.error.URLError as e:
            if attempt < max_retries - 1:
                backoff = 2 ** attempt + random.uniform(0, 1)
                logger.warning(f"URLError for {url}: {e.reason}, 退避 {backoff:.1f}s 后重试")
                time.sleep(backoff)
                continue
            logger.warning(f"URLError for {url}: {e.reason}")
            return -1, None
        except Exception as e:
            if attempt < max_retries - 1:
                backoff = 2 ** attempt + random.uniform(0, 1)
                logger.error(f"Unexpected error fetching {url}: {type(e).__name__}: {e}, 退避 {backoff:.1f}s 后重试")
                time.sleep(backoff)
                continue
            logger.error(f"Unexpected error fetching {url}: {type(e).__name__}: {e}")
            return -2, None
    return -1, None


def extract_links(html):
    """从HTML中提取所有链接"""
    parser = LinkExtractor()
    try:
        parser.feed(html)
    except Exception as e:
        logger.warning(f"HTML解析失败: {type(e).__name__}: {e}")
    return parser.links


def check_link_status(url):
    """仅检测链接的HTTP状态码（不解析内容），支持429/403/503限流退避重试"""
    headers = get_random_headers()
    headers['Accept'] = '*/*'
    max_retries = 3
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, method='HEAD', headers=headers)
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT, context=ssl_ctx) as resp:
                return resp.status
        except urllib.error.HTTPError as e:
            # 对 429/403/503 进行指数退避重试
            if e.code in (429, 403, 503) and attempt < max_retries - 1:
                backoff = 2 ** attempt + random.uniform(0, 1)
                logger.warning(f"HTTP {e.code} for {url}, 退避 {backoff:.1f}s 后重试 (第{attempt + 1}次)")
                time.sleep(backoff)
                headers = get_random_headers()
                headers['Accept'] = '*/*'
                continue
            return e.code
        except Exception:
            # HEAD 请求失败时尝试 GET
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT, context=ssl_ctx) as resp:
                    return resp.status
            except urllib.error.HTTPError as e:
                if e.code in (429, 403, 503) and attempt < max_retries - 1:
                    backoff = 2 ** attempt + random.uniform(0, 1)
                    logger.warning(f"HTTP {e.code} for {url}, 退避 {backoff:.1f}s 后重试 (第{attempt + 1}次)")
                    time.sleep(backoff)
                    headers = get_random_headers()
                    headers['Accept'] = '*/*'
                    continue
                return e.code
            except Exception as e:
                if attempt < max_retries - 1:
                    backoff = 2 ** attempt + random.uniform(0, 1)
                    logger.warning(f"请求失败 {url}: {type(e).__name__}, 退避 {backoff:.1f}s 后重试")
                    time.sleep(backoff)
                    continue
                logger.error(f"请求最终失败 {url}: {type(e).__name__}: {e}")
                return -1
    return -1


def crawl_and_check():
    """主函数：爬取网站并检测所有站内链接"""
    visited = set()        # 已访问的URL
    to_visit = deque()     # 待访问的URL队列
    all_links = {}         # 所有发现的链接: url -> 来源页面列表
    broken_links = {}      # 坏链: url -> (状态码, 来源页面列表)
    resource_links = set() # 资源链接（非页面，如PDF等）
    page_count = 0

    # 从首页开始
    to_visit.append(BASE_URL)

    logger.info("=" * 70)
    logger.info(f"网站链接检测工具 - 目标: {BASE_URL}")
    logger.info(f"最大爬取页面数: {MAX_PAGES}")
    logger.info("=" * 70)

    # ===== 第一阶段：爬取页面，收集站内链接 =====
    logger.info("【阶段1】爬取页面，收集站内链接...")
    logger.info("-" * 50)

    while to_visit and page_count < MAX_PAGES:
        url = to_visit.popleft()

        # 标准化URL
        url = normalize_url(url)
        if not url:
            continue

        # 已访问则跳过
        if url in visited:
            continue

        # 标记为已访问
        visited.add(url)

        # 资源链接：用HEAD请求检测可访问性，不下载内容
        if is_resource_url(url):
            resource_links.add(url)
            status = check_link_status(url)
            if status >= 400 or status < 0:
                # 从 all_links 获取来源，若不存在则标记为独立发现
                sources = all_links.get(url, {'独立发现（无页面引用）'})
                broken_links[url] = (status, sources)
                logger.warning(f"    ⚠ 资源链接不可访问: {url} -> {status}")
            else:
                logger.info(f"    ✓ 资源链接可访问: {url}")
            continue

        page_count += 1

        logger.info(f"  [{page_count}/{MAX_PAGES}] 正在爬取: {url}")

        status, html = fetch_url(url)

        if status >= 400 or status < 0:
            # 页面本身就是坏链
            broken_links[url] = (status, all_links.get(url, {'起始页面'}))
            logger.warning(f"    ⚠ 状态码: {status}")
            continue

        if html is None:
            # 非HTML内容（如某些无扩展名但实际返回PDF的URL）
            # 若状态码正常但内容非HTML，视为可访问的资源链接
            if status < 400 and status >= 0:
                resource_links.add(url)
                logger.info(f"    ✓ 返回非HTML内容，视为可访问资源: {url}")
            continue

        # 提取链接
        links = extract_links(html)
        new_count = 0
        for link in links:
            if not is_internal_link(link):
                continue

            normalized = normalize_url(link)
            if not normalized:
                continue

            # 记录链接来源（使用set去重避免同一页面多次引用导致列表膨胀）
            if normalized not in all_links:
                all_links[normalized] = set()
            all_links[normalized].add(url)

            # 资源链接单独记录，但仍加入待检测队列
            if is_resource_url(normalized):
                resource_links.add(normalized)
                # 资源链接也需要检测可访问性，继续加入队列

            # 加入待访问队列
            if normalized not in visited and normalized not in to_visit:
                to_visit.append(normalized)
                new_count += 1

        logger.info(f"    发现 {len(links)} 个链接，其中 {new_count} 个新的站内页面链接")

        # 请求间隔（添加随机抖动，模拟人类行为）
        delay = REQUEST_DELAY + random.uniform(0, 0.2)
        time.sleep(delay)

    logger.info(f"爬取完成。共访问 {page_count} 个页面，发现 {len(all_links)} 个站内链接，"
                f"{len(resource_links)} 个资源链接")

    # ===== 第二阶段：检测未在爬取阶段成功访问的链接 =====
    logger.info("【阶段2】检测未在爬取阶段成功访问的链接...")
    logger.info("-" * 50)

    # 找出所有需要检测的链接（页面链接 + 资源链接）
    all_urls_to_check = set(all_links.keys()) | resource_links
    # 已在爬取阶段成功访问的URL（状态码正常）不需要再检测
    # 但爬取阶段失败的（网络错误 status < 0）需要重新检测
    successfully_visited = {url for url in visited if url not in broken_links}
    urls_to_check = all_urls_to_check - successfully_visited

    total = len(urls_to_check)
    checked = 0

    for url in sorted(urls_to_check):
        checked += 1
        status = check_link_status(url)

        status_text = f"{status}" if status > 0 else "连接失败"
        is_ok = 200 <= status < 400

        if not is_ok:
            broken_links[url] = (status, all_links.get(url, {'未知来源'}))
            logger.warning(f"  [{checked}/{total}] ✗ {url} -> {status_text}")
        else:
            logger.info(f"  [{checked}/{total}] ✓ {url} -> {status_text}")

        # 第二阶段请求间隔也添加随机抖动
        delay = REQUEST_DELAY * 0.5 + random.uniform(0, 0.1)
        time.sleep(delay)

    # ===== 输出结果 =====
    logger.info("=" * 70)
    logger.info("检测结果汇总")
    logger.info("=" * 70)
    logger.info(f"  爬取页面数:     {page_count}")
    logger.info(f"  站内页面链接:   {len(all_links)}")
    logger.info(f"  资源链接:       {len(resource_links)}")
    logger.info(f"  坏链/断链数量:  {len(broken_links)}")
    logger.info(f"  （含资源链接坏链: {sum(1 for url in broken_links if is_resource_url(url))} 个）")

    if broken_links:
        logger.info("=" * 70)
        logger.info("坏链/断链详情")
        logger.info("=" * 70)

        for url, (status, sources) in sorted(broken_links.items()):
            status_text = f"HTTP {status}" if status > 0 else "连接失败"
            logger.warning(f"  URL: {url}")
            logger.warning(f"  状态: {status_text}")
            logger.warning(f"  来源页面:")
            for src in list(sources)[:5]:  # 最多显示5个来源
                logger.warning(f"    - {src}")
            if len(sources) > 5:
                logger.warning(f"    ... 还有 {len(sources) - 5} 个来源页面")

        # 输出CSV报告
        csv_path = CSV_OUTPUT
        with open(csv_path, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f)
            writer.writerow(['坏链URL', '状态码', '链接类型', '来源页面1', '来源页面2', '来源页面3', '来源页面4', '来源页面5'])
            for url, (status, sources) in sorted(broken_links.items()):
                status_text = f"HTTP {status}" if status > 0 else "连接失败"
                link_type = '资源链接' if is_resource_url(url) else '页面链接'
                source_list = list(sources)[:5]
                # 补齐5列
                source_list += [''] * (5 - len(source_list))
                writer.writerow([url, status_text, link_type] + source_list)
        logger.info(f"  坏链报告已输出到: {csv_path}")
    else:
        logger.info("  🎉 所有站内链接均可正常访问！")

    # 输出所有链接及其来源到CSV
    all_links_csv_path = ALL_LINKS_OUTPUT
    with open(all_links_csv_path, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        writer.writerow(['链接URL', '链接类型', '来源页面1', '来源页面2', '来源页面3', '来源页面4', '来源页面5'])
        # 合并页面链接和资源链接
        all_urls = set(all_links.keys()) | resource_links
        for url in sorted(all_urls):
            link_type = '资源链接' if is_resource_url(url) else '页面链接'
            sources = all_links.get(url, {'独立发现（无页面引用）'})
            source_list = list(sources)[:5]
            source_list += [''] * (5 - len(source_list))
            writer.writerow([url, link_type] + source_list)
    logger.info(f"  所有链接报告已输出到: {all_links_csv_path}")

    logger.info("=" * 70)
    logger.info("检测完成")
    logger.info("=" * 70)

    return broken_links


if __name__ == '__main__':
    try:
        broken = crawl_and_check()
        sys.exit(1 if broken else 0)
    except KeyboardInterrupt:
        logger.info("用户中断检测")
        sys.exit(130)
    except Exception as e:
        logger.error(f"检测过程发生未预期错误: {type(e).__name__}: {e}")
        sys.exit(1)
