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
  --sources       选择图片源: flickr,inat,gbif,wikimedia (默认: 全部)
  --no-zip        不打包 ZIP
  --min-size      最小图片尺寸 px (默认: 200)
"""

import os
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

# ── 尝试导入可选依赖 ────────────────────────────────────────────────────────
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

# ── 日志配置 ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("fish-scraper")

# ═══════════════════════════════════════════════════════════════════════════════
# 分类过滤白名单 / 黑名单
# ═══════════════════════════════════════════════════════════════════════════════

# iNaturalist taxon_id 白名单 — 只允许这些大类（鱼纲）
# 47178 = Ray-finned Fishes (Actinopterygii)
# 85497 = Cartilaginous Fishes (Chondrichthyes)
# 48921 = Jawless Fishes (Agnatha)
INAT_FISH_TAXON_IDS = "47178,85497,48921"

# GBIF classKey 白名单（硬骨鱼纲、软骨鱼纲）
GBIF_CLASS_KEYS = [
    "204",    # Actinopterygii 辐鳍鱼纲
    "133",    # Chondrichthyes 软骨鱼纲
    "11592",  # Sarcopterygii  肉鳍鱼纲
]

# 被观测物种名中禁止出现的词（排除鸟、哺乳、昆虫、植物、真菌等）
NON_FISH_TAXON_KEYWORDS = {
    # 鸟类
    "aves", "bird", "passerine", "raptor", "waterfowl",
    # 哺乳
    "mammalia", "mammal", "rodent", "primate",
    # 爬行/两栖
    "reptilia", "reptile", "amphibia", "amphibian", "serpentes",
    "snake", "frog", "toad", "lizard", "turtle", "crocodil",
    # 昆虫/蛛形
    "insecta", "insect", "arachnida", "spider", "lepidoptera",
    "coleoptera", "diptera", "hymenoptera",
    # 软体/甲壳（非鱼水生）
    "mollusca", "mollusc", "crustacea", "crustacean",
    # 植物 ← 新增
    "plantae", "plant", "flora", "botany", "angiosperm",
    "gymnosperm", "moss", "fern", "grass", "herb", "shrub",
    "tree", "flower", "leaf", "root", "stem", "petal", "sepal",
    "pollen", "seed", "fruit", "vegetable", "algae", "seaweed",
    # 真菌 ← 新增
    "fungi", "fungus", "mycology", "mushroom", "mold", "mould",
    "lichen", "yeast", "basidiomycota", "ascomycota",
    # 宠物/家畜（防止误匹配）
    "cat", "dog", "horse", "cow", "pig", "sheep", "goat",
}

# ═══════════════════════════════════════════════════════════════════════════════
# 鱼病关键词配置
# ═══════════════════════════════════════════════════════════════════════════════
FISH_DISEASES = {
    "白点病 (Ich)":          ["ichthyophthirius multifiliis", "fish ich white spot"],
    "烂尾病 (Fin Rot)":      ["fish fin rot bacterial", "pseudomonas fin rot fish"],
    "水霉病 (Saprolegnia)":  ["saprolegnia fish fungal", "fish saprolegniosis"],
    "细菌性溃疡":             ["fish bacterial ulcer skin lesion", "fish aeromonas ulcer"],
    "锚头鳋 (Anchor Worm)": ["lernaea fish parasite", "fish anchor worm lernaeosis"],
    "鱼虱 (Fish Lice)":     ["argulus fish lice parasite", "fish argulosis"],
    "竖鳞病 (Dropsy)":      ["fish dropsy ascites swollen", "fish kidney disease edema"],
    "出血病 (Hemorrhage)":   ["fish hemorrhagic septicemia", "fish erythrodermatitis hemorrhage"],
    "鳃病 (Gill Disease)":  ["fish gill disease pathology", "fish bacterial gill disease"],
    "眼病 (Pop-eye)":        ["fish exophthalmia popeye", "fish eye infection exophthalmos"],
    "肠炎 (Enteritis)":     ["fish enteritis aeromonas intestinal", "fish bacterial enteritis"],
    "传染性造血器官坏死":      ["infectious hematopoietic necrosis salmon IHN", "IHN virus salmonid"],
    "病毒性出血性败血症":      ["viral hemorrhagic septicemia VHS fish", "VHS novirhabdovirus fish"],
    "鲤春病毒血症":           ["spring viremia carp SVC rhabdovirus", "carp SVC hemorrhagic"],
    "健康鱼对照":             ["healthy koi carp aquarium", "healthy trout salmon aquaculture"],
}

# ═══════════════════════════════════════════════════════════════════════════════
# 第一层过滤：API 请求时锁定鱼类分类 ID
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_inat_images(query: str, max_count: int) -> list[dict]:
    """
    iNaturalist API
    关键修复: taxon_id 锁定辐鳍鱼 / 软骨鱼 / 无颌鱼，不再依赖模糊的 taxon_name
    """
    results = []
    page, per_page = 1, min(max_count, 30)
    while len(results) < max_count:
        url = (
            "https://api.inaturalist.org/v1/observations?"
            f"q={urllib.parse.quote(query)}"
            f"&taxon_id={INAT_FISH_TAXON_IDS}"   # ← 锁定鱼类 taxon ID
            "&has[]=photos&quality_grade=research"
            f"&per_page={per_page}&page={page}"
            "&order=desc&order_by=created_at"
        )
        try:
            data = _json_get(url)
            items = data.get("results", [])
            if not items:
                break
            for obs in items:
                # 第二层校验：观测记录的 taxon 名称不得含非鱼关键词
                taxon = obs.get("taxon") or {}
                taxon_name_lower = (taxon.get("name", "") + " " + taxon.get("iconic_taxon_name", "")).lower()
                if any(bad in taxon_name_lower for bad in NON_FISH_TAXON_KEYWORDS):
                    log.debug(f"iNat skip non-fish taxon: {taxon.get('name')}")
                    continue
                # iconic_taxon_name 必须是 Actinopterygii / 鱼相关
                iconic = taxon.get("iconic_taxon_name", "")
                if iconic and iconic not in ("Actinopterygii", "Chondrichthyes", ""):
                    log.debug(f"iNat skip iconic={iconic}")
                    continue
                for photo in obs.get("photos", []):
                    img_url = photo.get("url", "").replace("square", "large")
                    if img_url:
                        results.append({
                            "url": img_url,
                            "source": "iNaturalist",
                            "obs_id": obs.get("id"),
                            "taxon": taxon.get("name", ""),
                            "license": photo.get("license_code", "unknown"),
                        })
                        if len(results) >= max_count:
                            return results
            page += 1
            time.sleep(0.5)
        except Exception as e:
            log.debug(f"iNat error: {e}")
            break
    return results


def fetch_gbif_images(query: str, max_count: int) -> list[dict]:
    """
    GBIF API
    关键修复: classKey 锁定硬骨鱼纲 / 软骨鱼纲，排除鸟类等
    """
    results = []
    # 对每个 classKey 分别请求，合并结果
    per_class = max(max_count // len(GBIF_CLASS_KEYS) + 5, 10)
    for class_key in GBIF_CLASS_KEYS:
        offset, limit = 0, min(per_class, 25)
        while len(results) < max_count:
            url = (
                "https://api.gbif.org/v1/occurrence/search?"
                f"q={urllib.parse.quote(query)}"
                "&mediaType=StillImage"
                "&kingdom=Animalia"         # ← 排除植物/真菌 kingdom
                f"&classKey={class_key}"    # ← 锁定鱼纲分类
                f"&limit={limit}&offset={offset}"
            )
            try:
                data = _json_get(url)
                items = data.get("results", [])
                if not items:
                    break
                for occ in items:
                    # 第二层校验：kingdom 必须是 Animalia，class/order 黑名单过滤
                    occ_kingdom = (occ.get("kingdom", "") or "").lower()
                    if occ_kingdom and occ_kingdom != "animalia":
                        log.debug(f"GBIF skip kingdom={occ_kingdom}")
                        continue
                    occ_class = (occ.get("class", "") or "").lower()
                    occ_order = (occ.get("order", "") or "").lower()
                    occ_family = (occ.get("family", "") or "").lower()
                    if any(bad in occ_class for bad in NON_FISH_TAXON_KEYWORDS):
                        continue
                    if any(bad in occ_order for bad in NON_FISH_TAXON_KEYWORDS):
                        continue
                    if any(bad in occ_family for bad in NON_FISH_TAXON_KEYWORDS):
                        continue
                    for media in occ.get("media", []):
                        img_url = media.get("identifier", "")
                        if img_url and img_url.startswith("http"):
                            results.append({
                                "url": img_url,
                                "source": "GBIF",
                                "key": occ.get("key"),
                                "taxon": occ.get("species", occ.get("genus", "")),
                                "license": media.get("license", "unknown"),
                            })
                            if len(results) >= max_count:
                                return results
                offset += limit
                time.sleep(0.4)
            except Exception as e:
                log.debug(f"GBIF error (classKey={class_key}): {e}")
                break
    return results


def fetch_flickr_images(query: str, max_count: int, api_key: str = "") -> list[dict]:
    """
    Flickr
    关键修复: 搜索词强制加 'fish' 前缀，避免纯疾病词匹配到其他动物
    """
    api_key = api_key or os.environ.get("FLICKR_API_KEY", "")
    # 确保查询词包含 fish
    if "fish" not in query.lower() and "salmon" not in query.lower() \
            and "carp" not in query.lower() and "trout" not in query.lower():
        query = "fish " + query
    results = []
    if api_key:
        page, per_page = 1, min(max_count, 50)
        while len(results) < max_count:
            url = (
                "https://www.flickr.com/services/rest/?"
                "method=flickr.photos.search"
                f"&api_key={api_key}"
                f"&text={urllib.parse.quote(query)}"
                "&license=1,2,3,4,5,6,9,10"
                "&content_type=1&media=photos"
                f"&per_page={per_page}&page={page}"
                "&extras=url_l,url_m,license,tags"
                "&format=json&nojsoncallback=1"
            )
            try:
                data = _json_get(url)
                photos = data.get("photos", {}).get("photo", [])
                if not photos:
                    break
                for p in photos:
                    # 第二层：标签中含非鱼关键词则跳过
                    tags = (p.get("tags", "") or "").lower()
                    if any(bad in tags for bad in NON_FISH_TAXON_KEYWORDS):
                        continue
                    img_url = p.get("url_l") or p.get("url_m", "")
                    if img_url:
                        results.append({
                            "url": img_url,
                            "source": "Flickr",
                            "photo_id": p.get("id"),
                            "license": p.get("license", "unknown"),
                        })
                        if len(results) >= max_count:
                            return results
                page += 1
                time.sleep(0.5)
            except Exception as e:
                log.debug(f"Flickr API error: {e}")
                break
    else:
        tag = urllib.parse.quote(query.replace(" ", ","))
        url = f"https://www.flickr.com/services/feeds/photos_public.gne?tags={tag}&format=json&nojsoncallback=1"
        try:
            data = _json_get(url)
            for item in data.get("items", [])[:max_count]:
                img = item.get("media", {}).get("m", "")
                if img:
                    results.append({
                        "url": img.replace("_m.", "_b."),
                        "source": "Flickr-RSS",
                        "license": "CC",
                    })
        except Exception as e:
            log.debug(f"Flickr RSS error: {e}")
    return results


def fetch_wikimedia_images(query: str, max_count: int) -> list[dict]:
    """
    Wikimedia Commons
    关键修复: 搜索词加 'fish' 限定 + 分类名黑名单过滤
    """
    if "fish" not in query.lower() and "salmon" not in query.lower() \
            and "carp" not in query.lower() and "trout" not in query.lower():
        query = "fish " + query
    results = []
    url = (
        "https://commons.wikimedia.org/w/api.php?"
        "action=query&generator=search&prop=imageinfo|categories"
        f"&gsrsearch={urllib.parse.quote(query)}"
        f"&gsrnamespace=6&gsrlimit={min(max_count, 20)}"
        "&iiprop=url|extmetadata&iiurlwidth=800"
        "&cllimit=5"
        "&format=json"
    )
    try:
        data = _json_get(url)
        pages = data.get("query", {}).get("pages", {})
        for page in pages.values():
            # 第二层：分类名不得含非鱼关键词
            cats = [c.get("title", "").lower() for c in page.get("categories", [])]
            if any(bad in cat for cat in cats for bad in NON_FISH_TAXON_KEYWORDS):
                log.debug(f"Wikimedia skip non-fish categories: {cats[:3]}")
                continue
            # 额外：标题/描述不得是植物标本
            page_title = page.get("title", "").lower()
            if any(bad in page_title for bad in ("plantae", "plant", "flower", "fungi", "mushroom", "algae", "seaweed")):
                log.debug(f"Wikimedia skip non-fish title: {page.get('title')}")
                continue
            info = page.get("imageinfo", [{}])[0]
            img_url = info.get("thumburl") or info.get("url", "")
            if img_url and img_url.lower().split("?")[0].endswith((".jpg", ".jpeg", ".png", ".webp")):
                license_info = info.get("extmetadata", {}).get("License", {}).get("value", "unknown")
                results.append({
                    "url": img_url,
                    "source": "Wikimedia",
                    "page_id": page.get("pageid"),
                    "license": license_info,
                })
                if len(results) >= max_count:
                    break
    except Exception as e:
        log.debug(f"Wikimedia error: {e}")
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# 第三层过滤：下载后用 PIL EXIF + 文件名关键词二次剔除
# ═══════════════════════════════════════════════════════════════════════════════

# URL / 文件名中出现这些词则直接跳过下载（路径前缀匹配）
URL_BLACKLIST_KEYWORDS = {
    # 动物非鱼
    "bird", "aves", "mammal", "reptil", "amphibi", "insect",
    "spider", "snake", "frog", "cat", "dog", "horse", "cow",
    # 植物 ← 新增
    "flower", "plant", "plantae", "flora", "botanical",
    "tree", "grass", "herb", "shrub", "moss", "fern",
    "seaweed", "algae", "pollen", "vegetabl",
    # 真菌 ← 新增
    "fungi", "fungus", "mushroom", "mold", "lichen",
    "mycolog", "basidiomyc", "ascomyc",
}

def url_looks_like_fish(url: str) -> bool:
    """简单检查 URL / 文件名是否含非鱼黑名单词"""
    url_lower = url.lower()
    for bad in URL_BLACKLIST_KEYWORDS:
        if bad in url_lower:
            return False
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# 核心工具函数
# ═══════════════════════════════════════════════════════════════════════════════

def _json_get(url: str, timeout: int = 15) -> dict:
    headers = {"User-Agent": "FishDiseaseDatasetBot/1.0 (research; contact@example.com)"}
    if HAS_REQUESTS:
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    else:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())


def download_image(url: str, dest_path: Path, min_size: int = 200) -> bool:
    """下载单张图片，含尺寸过滤"""
    # 第三层 URL 过滤
    if not url_looks_like_fish(url):
        log.debug(f"URL blacklist skip: {url[:60]}")
        return False

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; FishDiseaseBot/1.0)",
        "Accept": "image/*,*/*",
    }
    try:
        if HAS_REQUESTS:
            resp = requests.get(url, headers=headers, timeout=20, stream=True)
            resp.raise_for_status()
            content = resp.content
            content_type = resp.headers.get("Content-Type", "")
        else:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as r:
                content = r.read()
                content_type = r.headers.get("Content-Type", "")

        if not any(t in content_type for t in ["image/jpeg", "image/png", "image/webp", "image/"]):
            if not url.lower().split("?")[0].endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
                return False

        if HAS_PIL and min_size > 0:
            try:
                img = Image.open(_io.BytesIO(content))
                w, h = img.size
                if w < min_size or h < min_size:
                    return False
                if img.mode not in ("RGB", "L"):
                    img = img.convert("RGB")
                out = _io.BytesIO()
                img.save(out, format="JPEG", quality=90)
                content = out.getvalue()
                dest_path = dest_path.with_suffix(".jpg")
            except Exception:
                pass

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(content)
        return True

    except Exception as e:
        log.debug(f"Download failed [{url[:60]}]: {e}")
        return False


def safe_filename(name: str, idx: int, ext: str = ".jpg") -> str:
    h = hashlib.md5(name.encode()).hexdigest()[:6]
    return f"{idx:04d}_{h}{ext}"


def create_zip(dataset_dir: Path, output_path: Path) -> Path:
    log.info(f"📦 正在打包 ZIP: {output_path.name} ...")
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for f in sorted(dataset_dir.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(dataset_dir.parent))
    size_mb = output_path.stat().st_size / 1024 / 1024
    log.info(f"✅ ZIP 打包完成: {output_path} ({size_mb:.1f} MB)")
    return output_path


# ═══════════════════════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════════════════════

def collect_disease(
    disease_name: str,
    keywords: list[str],
    out_dir: Path,
    max_count: int,
    sources: list[str],
    min_size: int,
) -> dict:
    safe_name = disease_name.replace("/", "_").replace(" ", "_").replace("(", "").replace(")", "")
    class_dir = out_dir / safe_name
    class_dir.mkdir(parents=True, exist_ok=True)

    log.info(f"🔍 [{disease_name}] 开始搜索 (目标: {max_count} 张)")
    candidates = []

    for kw in keywords:
        per_src = max(max_count // max(len(sources), 1) + 5, 10)
        if "inat" in sources:
            candidates.extend(fetch_inat_images(kw, per_src))
        if "gbif" in sources:
            candidates.extend(fetch_gbif_images(kw, per_src))
        if "flickr" in sources:
            candidates.extend(fetch_flickr_images(kw, per_src))
        if "wikimedia" in sources:
            candidates.extend(fetch_wikimedia_images(kw, per_src))

    # 去重
    seen_urls = set()
    unique = []
    for c in candidates:
        if c["url"] not in seen_urls:
            seen_urls.add(c["url"])
            unique.append(c)

    log.info(f"  → 找到 {len(unique)} 个候选 URL，开始下载...")

    downloaded = 0
    metadata = []

    def _dl(item, idx):
        ext = ".jpg"
        fname = safe_filename(item["url"], idx, ext)
        dest = class_dir / fname
        if dest.exists():
            return item, fname, True
        ok = download_image(item["url"], dest, min_size)
        return item, dest.name, ok

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(_dl, item, i): item for i, item in enumerate(unique[:max_count * 2])}
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
                if downloaded % 10 == 0:
                    log.info(f"  ✓ [{disease_name}] 已下载 {downloaded}/{max_count}")

    meta_path = class_dir / "_metadata.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump({"disease": disease_name, "count": downloaded, "images": metadata}, f, ensure_ascii=False, indent=2)

    log.info(f"  ✅ [{disease_name}] 完成: {downloaded} 张")
    return {"disease": disease_name, "count": downloaded, "dir": str(class_dir)}


def build_dataset_index(dataset_dir: Path, results: list[dict]):
    index = {
        "created_at": datetime.now().isoformat(),
        "tool": "fish_disease_scraper",
        "total_images": sum(r["count"] for r in results),
        "classes": len(results),
        "class_distribution": {r["disease"]: r["count"] for r in results},
        "filter_layers": [
            "Layer 1 - API: iNat taxon_id 锁定鱼类, GBIF classKey 锁定鱼纲",
            "Layer 2 - Metadata: 观测记录 taxon/class/tags 非鱼关键词黑名单",
            "Layer 3 - URL: 下载前检查 URL 路径非鱼关键词",
        ],
        "usage_note": "本数据集仅供学术研究和 AI 模型训练使用。",
    }
    index_path = dataset_dir / "dataset_index.json"
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)

    readme = f"""# 🐟 Fish Disease Image Dataset

> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}  
> 总图片数: **{index['total_images']}** | 疾病类别: **{index['classes']}**

## 过滤机制（三层防护）

| 层 | 方式 | 说明 |
|----|------|------|
| API 层 | taxon_id / classKey | iNat 锁定辐鳍鱼 ID，GBIF 锁定硬骨/软骨鱼纲 |
| 元数据层 | 黑名单关键词 | 过滤观测记录中含鸟、哺乳、昆虫等词的条目 |
| URL 层 | 路径关键词 | 下载前检查 URL 是否含非鱼物种词 |

## 类别分布

| 疾病 | 图片数 |
|------|--------|
"""
    for d, c in index["class_distribution"].items():
        readme += f"| {d} | {c} |\n"

    readme += "\n## 使用许可\n\n仅供**学术研究和 AI 模型训练**使用，请遵守各来源平台许可协议。\n"
    (dataset_dir / "README.md").write_text(readme, encoding="utf-8")
    log.info(f"📄 数据集索引已生成: {index_path}")


# ═══════════════════════════════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════════════════════════════

def parse_args():
    parser = argparse.ArgumentParser(description="🐟 鱼病图片数据集自动收集工具")
    parser.add_argument("--diseases", help="指定疾病(中文名)，逗号分隔；不填则收集全部")
    parser.add_argument("--max-per-class", type=int, default=50)
    parser.add_argument("--output-dir", default="fish_disease_dataset")
    parser.add_argument("--sources", default="inat,gbif,flickr,wikimedia")
    parser.add_argument("--no-zip", action="store_true")
    parser.add_argument("--min-size", type=int, default=200)
    parser.add_argument("--flickr-key", default="")
    return parser.parse_args()


def check_dependencies():
    if not HAS_REQUESTS:
        log.error("缺少依赖 requests，请运行: pip install requests")
        sys.exit(1)
    if not HAS_PIL:
        log.warning("⚠️  未安装 Pillow，图片尺寸过滤已禁用 (pip install Pillow)")


def main():
    args = parse_args()
    check_dependencies()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    sources = [s.strip().lower() for s in args.sources.split(",")]

    if args.diseases:
        selected_names = [d.strip() for d in args.diseases.split(",")]
        diseases = {k: v for k, v in FISH_DISEASES.items() if any(n in k for n in selected_names)}
        if not diseases:
            log.error("未匹配到任何疾病，请检查名称")
            sys.exit(1)
    else:
        diseases = FISH_DISEASES

    log.info("=" * 60)
    log.info("🐟 鱼病图片数据集收集工具（三层过滤版）")
    log.info(f"   过滤层: API taxon_id | 元数据黑名单 | URL关键词")
    log.info(f"   疾病类别: {len(diseases)} 类  每类上限: {args.max_per_class} 张")
    log.info(f"   图片来源: {sources}")
    log.info(f"   输出目录: {out_dir.resolve()}")
    log.info("=" * 60)

    results = []
    for disease_name, keywords in diseases.items():
        result = collect_disease(
            disease_name=disease_name,
            keywords=keywords,
            out_dir=out_dir,
            max_count=args.max_per_class,
            sources=sources,
            min_size=args.min_size if HAS_PIL else 0,
        )
        results.append(result)
        time.sleep(1)

    build_dataset_index(out_dir, results)

    total = sum(r["count"] for r in results)
    log.info(f"\n{'='*60}")
    log.info(f"🎉 完成! 共 {total} 张，{len(results)} 个类别")
    log.info(f"{'='*60}\n")

    if not args.no_zip:
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        zip_path = Path(f"fish_disease_dataset_{ts}.zip")
        create_zip(out_dir, zip_path)
        print(f"\n📦 ZIP 已生成: {zip_path.resolve()}")
    else:
        print(f"\n📁 数据集目录: {out_dir.resolve()}")


if __name__ == "__main__":
    main()
