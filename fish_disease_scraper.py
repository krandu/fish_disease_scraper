#!/usr/bin/env python3
"""
Fish Disease Image Dataset Collector
=====================================
用途: 自动从多个公开图片源收集鱼病样本图片，用于 AI 识别数据集构建
支持: 本地运行 / GitHub Codespaces / GitHub Actions
输出: 按疾病分类整理的图片目录 + ZIP 压缩包

使用方法:
  pip install -r requirements.txt
  python fish_disease_scraper.py

可选参数:
  --diseases      指定要收集的疾病列表 (用逗号分隔)
  --max-per-class 每类最多收集图片数量 (默认: 50)
  --output-dir    输出目录 (默认: fish_disease_dataset)
  --sources       选择图片源: inat,gbif,wikimedia,flickr (默认: 全部)
  --no-zip        不打包 ZIP
  --min-size      最小图片尺寸 px (默认: 150)
"""

import os
import re
import sys
import time
import json
import zipfile
import hashlib
import logging
import argparse
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

try:
    from PIL import Image
    import io as _io
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("fish-scraper")

# ═══════════════════════════════════════════════════════════════════════════════
# 过滤配置  ── 设计原则：只在【分类学字段】做硬过滤，不碰 URL 路径
# ═══════════════════════════════════════════════════════════════════════════════

# iNaturalist taxon_id — 鱼类三大纲的根节点
# 47178 Actinopterygii 辐鳍鱼纲  85497 Chondrichthyes 软骨鱼纲  48921 Petromyzontida 无颌
INAT_FISH_TAXON_IDS = "47178,85497,48921"

# GBIF classKey — 只取鱼纲
GBIF_CLASS_KEYS = ["204", "133", "11592"]   # Actinopterygii / Chondrichthyes / Sarcopterygii

# iNat iconic_taxon_name 白名单（空字符串=未分类，保留）
INAT_ICONIC_WHITELIST = {"Actinopterygii", "Chondrichthyes", ""}

# 仅在分类学字段（taxon.name / class / order / family）做精确词匹配
# !! 不要把这些词用于 URL 过滤 —— URL 是纯数字 ID，不含物种词
TAXON_FIELD_BLACKLIST = frozenset({
    "aves", "mammalia", "reptilia", "amphibia",
    "insecta", "arachnida", "myriapoda",
    "plantae", "fungi", "chromista", "protozoa",
    "mollusca", "crustacea",
})

# ═══════════════════════════════════════════════════════════════════════════════
# 鱼病搜索词  ── 精简为学名/通用名，去掉冗余形容词减少噪声
# ═══════════════════════════════════════════════════════════════════════════════
FISH_DISEASES = {
    "白点病 (Ich)": [
        "ichthyophthirius multifiliis fish",
        "ich white spot fish disease",
    ],
    "烂尾病 (Fin Rot)": [
        "fin rot fish bacterial",
        "fish finnrot pseudomonas",
    ],
    "水霉病 (Saprolegnia)": [
        "saprolegnia fish infection",
        "fish water mold saprolegniasis",
    ],
    "细菌性溃疡": [
        "aeromonas fish ulcer",
        "fish skin ulcer bacterial lesion",
    ],
    "锚头鳋 (Anchor Worm)": [
        "lernaea fish parasite",
        "anchor worm fish copepod",
    ],
    "鱼虱 (Fish Lice)": [
        "argulus fish lice",
        "fish louse branchiura parasite",
    ],
    "竖鳞病 (Dropsy)": [
        "fish dropsy pinecone scales",
        "fish ascites edema scale protrusion",
    ],
    "出血病 (Hemorrhage)": [
        "fish hemorrhagic disease skin",
        "erythrodermatitis fish hemorrhage lesion",
    ],
    "鳃病 (Gill Disease)": [
        "fish gill disease pathology",
        "fish bacterial gill infection",
    ],
    "眼病 (Pop-eye)": [
        "fish exophthalmia popeye",
        "fish eye swelling exophthalmos",
    ],
    "肠炎 (Enteritis)": [
        "fish enteritis intestinal disease",
        "aeromonas fish gut infection",
    ],
    "传染性造血器官坏死 (IHN)": [
        "infectious hematopoietic necrosis fish",
        "IHN virus salmonid rhabdovirus",
    ],
    "病毒性出血性败血症 (VHS)": [
        "viral hemorrhagic septicemia fish",
        "VHS novirhabdovirus salmonid",
    ],
    "鲤春病毒血症 (SVC)": [
        "spring viremia carp rhabdovirus",
        "SVC carp hemorrhagic disease",
    ],
    "健康鱼对照": [
        "healthy koi carp fish",
        "healthy trout salmon aquaculture fish",
    ],
}

# ═══════════════════════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════════════════════

def _json_get(url: str, timeout: int = 20) -> dict:
    headers = {"User-Agent": "FishDiseaseDatasetBot/1.0 (academic research)"}
    if HAS_REQUESTS:
        r = requests.get(url, headers=headers, timeout=timeout)
        r.raise_for_status()
        return r.json()
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _taxon_is_fish(taxon_fields: list[str]) -> bool:
    """
    检查若干分类学字段，只要含黑名单词就拒绝。
    使用精确词边界匹配，避免 'plant' 误杀 'implant' 等。
    """
    combined = " ".join(f.lower() for f in taxon_fields if f)
    for bad in TAXON_FIELD_BLACKLIST:
        # 用词边界匹配：\bword\b
        if re.search(r'\b' + re.escape(bad) + r'\b', combined):
            return False
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# 数据源
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_inat_images(query: str, max_count: int) -> list[dict]:
    """
    iNaturalist — taxon_id 锁定鱼类三大纲，iconic_taxon_name 白名单二次校验
    quality_grade=any 放宽（research 太严会导致数量极少）
    """
    results = []
    page, per_page = 1, min(max_count, 200)
    while len(results) < max_count:
        url = (
            "https://api.inaturalist.org/v1/observations?"
            f"q={urllib.parse.quote(query)}"
            f"&taxon_id={INAT_FISH_TAXON_IDS}"
            "&has[]=photos"
            # 放开 quality_grade，不只限 research
            f"&per_page={per_page}&page={page}"
            "&order=desc&order_by=created_at"
        )
        try:
            data = _json_get(url)
            items = data.get("results", [])
            if not items:
                break
            for obs in items:
                taxon = obs.get("taxon") or {}
                iconic = taxon.get("iconic_taxon_name", "")
                if iconic not in INAT_ICONIC_WHITELIST:
                    continue
                taxon_name = taxon.get("name", "")
                if not _taxon_is_fish([taxon_name, iconic]):
                    continue
                for photo in obs.get("photos", []):
                    img_url = photo.get("url", "").replace("square", "large")
                    if img_url:
                        results.append({
                            "url": img_url,
                            "source": "iNaturalist",
                            "taxon": taxon_name,
                            "license": photo.get("license_code", "unknown"),
                        })
                        if len(results) >= max_count:
                            return results
            if len(items) < per_page:
                break
            page += 1
            time.sleep(0.4)
        except Exception as e:
            log.debug(f"iNat error: {e}")
            break
    return results


def fetch_gbif_images(query: str, max_count: int) -> list[dict]:
    """
    GBIF — kingdom=Animalia + classKey 双重锁定
    对每个 classKey 单独翻页，最大化召回
    """
    results = []
    per_class_target = max(max_count // len(GBIF_CLASS_KEYS) + 10, 20)
    for class_key in GBIF_CLASS_KEYS:
        offset, limit = 0, 25
        class_results = []
        while len(class_results) < per_class_target:
            url = (
                "https://api.gbif.org/v1/occurrence/search?"
                f"q={urllib.parse.quote(query)}"
                "&mediaType=StillImage"
                "&kingdom=Animalia"
                f"&classKey={class_key}"
                f"&limit={limit}&offset={offset}"
            )
            try:
                data = _json_get(url)
                items = data.get("results", [])
                if not items:
                    break
                for occ in items:
                    kingdom = (occ.get("kingdom") or "").lower()
                    if kingdom and kingdom != "animalia":
                        continue
                    taxon_fields = [
                        occ.get("class", ""),
                        occ.get("order", ""),
                        occ.get("family", ""),
                    ]
                    if not _taxon_is_fish(taxon_fields):
                        continue
                    for media in occ.get("media", []):
                        img_url = media.get("identifier", "")
                        if img_url and img_url.startswith("http"):
                            class_results.append({
                                "url": img_url,
                                "source": "GBIF",
                                "taxon": occ.get("species") or occ.get("genus", ""),
                                "license": media.get("license", "unknown"),
                            })
                if len(items) < limit:
                    break
                offset += limit
                time.sleep(0.3)
            except Exception as e:
                log.debug(f"GBIF error classKey={class_key}: {e}")
                break
        results.extend(class_results)
        if len(results) >= max_count:
            break
    return results


def fetch_wikimedia_images(query: str, max_count: int) -> list[dict]:
    """
    Wikimedia Commons — 搜索词加 fish 限定，分类标题做精确词过滤
    gsrlimit 提升到 50 增加召回
    """
    fish_anchors = ("fish", "salmon", "carp", "trout", "tilapia",
                    "catfish", "bass", "tuna", "cod", "herring",
                    "ichthyo", "piscis", "lernaea", "argulus")
    if not any(a in query.lower() for a in fish_anchors):
        query = "fish " + query

    results = []
    url = (
        "https://commons.wikimedia.org/w/api.php?"
        "action=query&generator=search&prop=imageinfo|categories"
        f"&gsrsearch={urllib.parse.quote(query)}"
        "&gsrnamespace=6"
        "&gsrlimit=50"
        "&iiprop=url|extmetadata&iiurlwidth=1000"
        "&cllimit=10"
        "&format=json"
    )
    try:
        data = _json_get(url)
        pages = data.get("query", {}).get("pages", {})
        for page in pages.values():
            # 页面标题过滤（精确词边界）
            title = page.get("title", "").lower()
            if not _taxon_is_fish([title]):
                log.debug(f"Wikimedia skip title: {page.get('title')}")
                continue
            # 分类标题过滤
            cats = [c.get("title", "").lower() for c in page.get("categories", [])]
            skip = False
            for cat in cats:
                if not _taxon_is_fish([cat]):
                    skip = True
                    break
            if skip:
                continue
            info = page.get("imageinfo", [{}])[0]
            img_url = info.get("thumburl") or info.get("url", "")
            if img_url and re.search(r'\.(jpe?g|png|webp)(\?|$)', img_url, re.I):
                license_val = info.get("extmetadata", {}).get("License", {}).get("value", "unknown")
                results.append({
                    "url": img_url,
                    "source": "Wikimedia",
                    "taxon": "",
                    "license": license_val,
                })
                if len(results) >= max_count:
                    break
    except Exception as e:
        log.debug(f"Wikimedia error: {e}")
    return results


def fetch_flickr_images(query: str, max_count: int) -> list[dict]:
    """
    Flickr — 需要 API Key (环境变量 FLICKR_API_KEY)
    无 Key 时跳过（避免爬公开 RSS 质量差）
    """
    api_key = os.environ.get("FLICKR_API_KEY", "")
    if not api_key:
        return []

    fish_anchors = ("fish", "salmon", "carp", "trout", "koi", "tilapia")
    if not any(a in query.lower() for a in fish_anchors):
        query = "fish " + query

    results = []
    page, per_page = 1, min(max_count, 100)
    while len(results) < max_count:
        url = (
            "https://www.flickr.com/services/rest/?"
            "method=flickr.photos.search"
            f"&api_key={api_key}"
            f"&text={urllib.parse.quote(query)}"
            "&license=1,2,3,4,5,6,9,10"
            "&content_type=1&media=photos"
            f"&per_page={per_page}&page={page}"
            "&extras=url_l,url_m,license,tags,description"
            "&format=json&nojsoncallback=1"
        )
        try:
            data = _json_get(url)
            photos = data.get("photos", {}).get("photo", [])
            if not photos:
                break
            for p in photos:
                tags = (p.get("tags") or "").lower()
                desc = (p.get("description", {}).get("_content") or "").lower()
                # 只过滤标签/描述字段，不过滤 URL
                if not _taxon_is_fish([tags, desc]):
                    continue
                img_url = p.get("url_l") or p.get("url_m", "")
                if img_url:
                    results.append({
                        "url": img_url,
                        "source": "Flickr",
                        "taxon": "",
                        "license": str(p.get("license", "unknown")),
                    })
                    if len(results) >= max_count:
                        return results
            if len(photos) < per_page:
                break
            page += 1
            time.sleep(0.5)
        except Exception as e:
            log.debug(f"Flickr error: {e}")
            break
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# 下载
# ═══════════════════════════════════════════════════════════════════════════════

def download_image(url: str, dest_path: Path, min_size: int = 150) -> bool:
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.google.com/",
    }
    try:
        if HAS_REQUESTS:
            resp = requests.get(url, headers=headers, timeout=25, stream=True,
                                allow_redirects=True)
            if resp.status_code in (403, 404, 410):
                return False
            resp.raise_for_status()
            content = resp.content
            content_type = resp.headers.get("Content-Type", "")
        else:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=25) as r:
                content = r.read()
                content_type = r.headers.get("Content-Type", "")

        # 必须是图片
        is_image = (
            "image/" in content_type
            or re.search(r'\.(jpe?g|png|webp|gif)(\?|$)', url, re.I)
        )
        if not is_image:
            return False

        # 尺寸过滤
        if HAS_PIL and min_size > 0 and len(content) > 100:
            try:
                img = Image.open(_io.BytesIO(content))
                w, h = img.size
                if w < min_size or h < min_size:
                    return False
                if img.mode not in ("RGB", "L"):
                    img = img.convert("RGB")
                buf = _io.BytesIO()
                img.save(buf, format="JPEG", quality=88)
                content = buf.getvalue()
                dest_path = dest_path.with_suffix(".jpg")
            except Exception:
                pass

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(content)
        return True

    except Exception as e:
        log.debug(f"Download failed [{url[:70]}]: {e}")
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# 主收集流程
# ═══════════════════════════════════════════════════════════════════════════════

def collect_disease(
    disease_name: str,
    keywords: list[str],
    out_dir: Path,
    max_count: int,
    sources: list[str],
    min_size: int,
) -> dict:
    safe_name = re.sub(r'[^\w\-]', '_', disease_name)
    class_dir = out_dir / safe_name
    class_dir.mkdir(parents=True, exist_ok=True)

    log.info(f"🔍 [{disease_name}] 开始搜索 (目标: {max_count} 张)")

    # 每个关键词、每个来源都搜，候选尽量多
    per_kw_src = max(max_count // max(len(keywords) * max(len(sources), 1), 1) + 20, 30)
    candidates = []
    for kw in keywords:
        if "inat" in sources:
            r = fetch_inat_images(kw, per_kw_src)
            log.debug(f"  iNat [{kw}]: {len(r)}")
            candidates.extend(r)
        if "gbif" in sources:
            r = fetch_gbif_images(kw, per_kw_src)
            log.debug(f"  GBIF [{kw}]: {len(r)}")
            candidates.extend(r)
        if "wikimedia" in sources:
            r = fetch_wikimedia_images(kw, per_kw_src)
            log.debug(f"  Wiki [{kw}]: {len(r)}")
            candidates.extend(r)
        if "flickr" in sources:
            r = fetch_flickr_images(kw, per_kw_src)
            log.debug(f"  Flickr [{kw}]: {len(r)}")
            candidates.extend(r)

    # URL 去重
    seen, unique = set(), []
    for c in candidates:
        if c["url"] not in seen:
            seen.add(c["url"])
            unique.append(c)

    log.info(f"  → 候选 {len(unique)} 个，开始并发下载...")

    downloaded = 0
    metadata = []

    def _dl(item: dict, idx: int):
        h = hashlib.md5(item["url"].encode()).hexdigest()[:8]
        dest = class_dir / f"{idx:04d}_{h}.jpg"
        if dest.exists():
            return item, dest.name, True
        ok = download_image(item["url"], dest, min_size)
        return item, dest.name, ok

    # 候选不够时发出警告
    if len(unique) < max_count:
        log.warning(f"  ⚠️  候选仅 {len(unique)} 个，可能不足 {max_count} 张目标")

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(_dl, item, i): item
            for i, item in enumerate(unique[: max_count * 3])  # 多取候选，以防下载失败
        }
        for fut in as_completed(futures):
            if downloaded >= max_count:
                break
            item, fname, ok = fut.result()
            if ok:
                downloaded += 1
                metadata.append({
                    "file": fname,
                    "url": item["url"],
                    "source": item.get("source", ""),
                    "taxon": item.get("taxon", ""),
                    "license": item.get("license", "unknown"),
                })
                if downloaded % 20 == 0:
                    log.info(f"  ✓ [{disease_name}] {downloaded}/{max_count}")

    (class_dir / "_metadata.json").write_text(
        json.dumps({"disease": disease_name, "count": downloaded, "images": metadata},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log.info(f"  ✅ [{disease_name}] 完成: {downloaded} 张")
    return {"disease": disease_name, "count": downloaded}


def build_dataset_index(dataset_dir: Path, results: list[dict]):
    total = sum(r["count"] for r in results)
    index = {
        "created_at": datetime.now().isoformat(),
        "tool": "fish_disease_scraper_v3",
        "total_images": total,
        "classes": len(results),
        "class_distribution": {r["disease"]: r["count"] for r in results},
        "filter_strategy": {
            "inat": "taxon_id 白名单 + iconic_taxon_name 白名单",
            "gbif": "kingdom=Animalia + classKey 白名单 + 分类字段词边界黑名单",
            "wikimedia": "搜索词 fish 锚定 + 页面标题/分类词边界黑名单",
            "flickr": "搜索词 fish 锚定 + 标签/描述词边界黑名单",
            "url_filter": "不过滤 URL 路径（URL 多为数字 ID，过滤会误杀）",
        },
    }
    (dataset_dir / "dataset_index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    readme = f"""# 🐟 Fish Disease Image Dataset

生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')} | 总图片: **{total}** | 类别: **{len(results)}**

## 类别分布

| 疾病 | 图片数 |
|------|--------|
"""
    for r in results:
        readme += f"| {r['disease']} | {r['count']} |\n"
    readme += "\n## 过滤策略\n\n- **API 层**：iNat `taxon_id` / GBIF `classKey` 白名单，从源头锁定鱼类\n"
    readme += "- **元数据层**：分类字段词边界黑名单（精确匹配，不误杀 URL 数字 ID）\n"
    readme += "- **不过滤 URL 路径**：图片 URL 多为纯数字，路径过滤只会误杀正确图片\n"
    (dataset_dir / "README.md").write_text(readme, encoding="utf-8")
    log.info(f"📄 数据集索引已生成")


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def parse_args():
    p = argparse.ArgumentParser(description="🐟 鱼病图片数据集自动收集工具 v3")
    p.add_argument("--diseases", help="指定疾病中文名，逗号分隔；不填=全部")
    p.add_argument("--max-per-class", type=int, default=50)
    p.add_argument("--output-dir", default="fish_disease_dataset")
    p.add_argument("--sources", default="inat,gbif,wikimedia,flickr")
    p.add_argument("--no-zip", action="store_true")
    p.add_argument("--min-size", type=int, default=150)
    return p.parse_args()


def main():
    args = parse_args()
    if not HAS_REQUESTS:
        log.error("缺少 requests，请: pip install requests")
        sys.exit(1)
    if not HAS_PIL:
        log.warning("⚠️  未安装 Pillow，图片尺寸过滤禁用 (pip install Pillow)")

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    sources = [s.strip().lower() for s in args.sources.split(",")]

    diseases = FISH_DISEASES
    if args.diseases:
        sel = [d.strip() for d in args.diseases.split(",")]
        diseases = {k: v for k, v in FISH_DISEASES.items() if any(s in k for s in sel)}
        if not diseases:
            log.error("未匹配到任何疾病，请检查名称")
            sys.exit(1)

    log.info("=" * 60)
    log.info("🐟 鱼病图片数据集收集工具 v3")
    log.info(f"   过滤策略: 分类字段词边界黑名单（不过滤 URL）")
    log.info(f"   疾病类别: {len(diseases)} 类  每类上限: {args.max_per_class} 张")
    log.info(f"   图片来源: {sources}")
    log.info(f"   输出目录: {out_dir.resolve()}")
    log.info("=" * 60)

    results = []
    for name, kws in diseases.items():
        r = collect_disease(name, kws, out_dir, args.max_per_class, sources,
                            args.min_size if HAS_PIL else 0)
        results.append(r)
        time.sleep(1)

    build_dataset_index(out_dir, results)
    total = sum(r["count"] for r in results)
    log.info(f"\n{'='*60}")
    log.info(f"🎉 完成! 共 {total} 张，{len(results)} 个类别")
    log.info(f"{'='*60}\n")

    if not args.no_zip:
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        zip_path = Path(f"fish_disease_dataset_{ts}.zip")
        log.info(f"📦 打包 ZIP: {zip_path}")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for f in sorted(out_dir.rglob("*")):
                if f.is_file():
                    zf.write(f, f.relative_to(out_dir.parent))
        log.info(f"✅ ZIP: {zip_path} ({zip_path.stat().st_size/1024/1024:.1f} MB)")
        print(f"\n📦 ZIP 已生成: {zip_path.resolve()}")
    else:
        print(f"\n📁 数据集目录: {out_dir.resolve()}")


if __name__ == "__main__":
    main()
