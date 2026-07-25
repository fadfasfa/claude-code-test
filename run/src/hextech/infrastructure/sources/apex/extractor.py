"""Apex 抓取职责拆分模块。"""
from __future__ import annotations

from hextech.infrastructure.sources.apex.common import (
    ARCHIVED_BOOL_KEYS,
    ARCHIVED_STATUS_KEYS,
    ARCHIVED_SYNERGY_MARKERS,
    ARCHIVED_TEXT_KEYS,
    Any,
    BUNDLE_INTERACTION_SECTION_MARKER,
    BeautifulSoup,
    ChampionInfo,
    FetchedResource,
    HYDRATION_PATTERN,
    Iterable,
    JSON_SCRIPT_PATTERN,
    Optional,
    Path,
    SYNERGY_TAG_LABELS,
    SynergyEntry,
    TIER_LABELS,
    VISIBLE_RATING_PATTERN,
    VISIBLE_STOP_LINE_PATTERN,
    _clean_text,
    _safe_exception_label,
    _sanitize_url_for_log,
    ast,
    json,
    logger,
    normalize_augment_name,
    normalize_name,
    normalize_slug,
    normalize_tag,
    normalize_tier,
    re,
    unescape,
    urlparse,
)
class SynergyExtractor:
    """从 HTML/JS/JSON 资源中提取结构化联动对象。"""

    def __init__(self, champion_lookup: dict[str, ChampionInfo], augment_name_map: dict[str, str]):
        self.champion_lookup = champion_lookup
        self.augment_name_map = augment_name_map
        self.archived_filtered_count = 0
        self.archived_filter_samples: list[dict[str, str]] = []

    def _record_archived_filter(self, *, reason: str, sample: str = "") -> None:
        self.archived_filtered_count += 1
        if len(self.archived_filter_samples) < 20:
            self.archived_filter_samples.append({
                "reason": reason,
                "sample": _clean_text(sample)[:180],
            })

    def extract(self, resources: Iterable[FetchedResource]) -> dict[str, list[SynergyEntry]]:
        results, errors = self.extract_with_diagnostics(resources)
        if results:
            return results

        raise ValueError("联动解析结果为空" + (f"；errors={';'.join(errors[:6])}" if errors else ""))

    def extract_with_diagnostics(
        self,
        resources: Iterable[FetchedResource],
    ) -> tuple[dict[str, list[SynergyEntry]], tuple[str, ...]]:
        """返回解析结果与有限错误，让详情页合法空态继续由页面契约判断。"""

        results: dict[str, list[SynergyEntry]] = {}
        errors: list[str] = []
        for resource in resources:
            try:
                for entry in self._extract_from_resource(resource):
                    results.setdefault(entry.champion_slug, []).append(entry)
            except Exception as exc:
                errors.append(f"{Path(urlparse(resource.url).path).name or resource.url}:{_safe_exception_label(exc)}")
                logger.debug("资源解析失败：%s", _sanitize_url_for_log(resource.url), exc_info=True)

        normalized = self._dedupe_entries(results) if results else {}
        return normalized, tuple(errors[:6])

    def _extract_from_resource(self, resource: FetchedResource) -> list[SynergyEntry]:
        text = resource.text or ""
        entries = []
        if "<html" in text[:1000].lower():
            entries.extend(self._extract_from_html(text, resource.url))
        if text.strip().startswith(("{", "[")):
            try:
                entries.extend(self._extract_from_json_payload(json.loads(text), fallback_slug=""))
                if entries:
                    return entries
            except json.JSONDecodeError:
                pass
        entries.extend(self._extract_old_bundle(text))
        entries.extend(self._extract_generic_js_objects(text))
        return entries

    def _extract_from_html(self, html: str, url: str) -> list[SynergyEntry]:
        entries = []
        for match in HYDRATION_PATTERN.findall(html) + JSON_SCRIPT_PATTERN.findall(html):
            try:
                payload = json.loads(unescape(match).strip())
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            entries.extend(self._extract_from_json_payload(payload, fallback_slug=self._slug_from_url(url)))
        entries.extend(self._extract_from_visible_html_text(html, url))
        return entries

    def _extract_from_visible_html_text(self, html: str, url: str) -> list[SynergyEntry]:
        soup = BeautifulSoup(html or "", "html.parser")
        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.decompose()
        lines = []
        for raw_line in soup.get_text("\n").splitlines():
            line = _clean_text(raw_line)
            if line and line not in lines[-3:]:
                lines.append(line)
        if not lines:
            return []

        url_slug = self._slug_from_url(url)
        fallback_slug = url_slug if url_slug in self.champion_lookup else (self._slug_from_visible_lines(lines) or url_slug)
        entries = []
        for index, line in enumerate(lines):
            rating_match = VISIBLE_RATING_PATTERN.match(line)
            if not rating_match:
                continue
            if self._visible_rating_belongs_to_archived_card(lines, index):
                self._record_archived_filter(
                    reason="visible_archived_card",
                    sample=" | ".join(lines[max(0, index - 6): min(len(lines), index + 8)]),
                )
                continue

            augment_names = []
            tier = ""
            cursor = index - 1
            while cursor >= 0 and len(augment_names) < 4:
                current = lines[cursor]
                if current == "关联套装":
                    cursor -= 1
                    continue
                if re.match(r"^关联\s*\d+\s*个海克斯$", current):
                    if augment_names:
                        break
                    cursor -= 1
                    continue
                normalized_tier = normalize_tier(current)
                if normalized_tier != current or current in TIER_LABELS:
                    tier = tier or normalized_tier
                    cursor -= 1
                    continue
                resolved_names = self._resolve_known_augment_names(current)
                if resolved_names and self._looks_like_augment_name(current, resolved_names):
                    augment_names = resolved_names + augment_names
                    cursor -= 1
                    continue
                if self._looks_like_visible_augment_name(current):
                    augment_names = [current] + augment_names
                    cursor -= 1
                    continue
                break

            if not augment_names:
                continue

            rating, tag = self._parse_visible_rating_tag(line, lines[index + 1:index + 6])
            author, content, is_original, upvotes, downvotes = self._parse_visible_author_content(lines, index + 1)
            if not content:
                continue
            if self._contains_archived_marker(content):
                self._record_archived_filter(
                    reason="visible_archived_content",
                    sample=content,
                )
                continue
            entries.append(SynergyEntry(
                champion_slug=fallback_slug,
                augment_names=list(dict.fromkeys(augment_names)),
                tier=tier or "黄金",
                rating=rating,
                tag=tag,
                author=author,
                is_original=is_original or "原创" in line.lower() or "original" in line.lower(),
                content=content,
                upvotes=upvotes,
                downvotes=downvotes,
            ))
        return [entry for entry in entries if entry.champion_slug]

    @staticmethod
    def _contains_archived_marker(value: Any) -> bool:
        if isinstance(value, dict):
            return any(SynergyExtractor._contains_archived_marker(child) for child in value.values())
        if isinstance(value, list):
            return any(SynergyExtractor._contains_archived_marker(child) for child in value)
        text = _clean_text(value).lower()
        if not text:
            return False
        return any(marker.lower() in text for marker in ARCHIVED_SYNERGY_MARKERS)

    def _visible_rating_belongs_to_archived_card(self, lines: list[str], rating_index: int) -> bool:
        before = lines[max(0, rating_index - 8): rating_index]
        after = lines[rating_index + 1: min(len(lines), rating_index + 8)]
        if any(self._contains_archived_marker(line) for line in before):
            return True
        if any(_clean_text(line) == "已弃用" for line in after):
            return True
        return False

    def _dict_has_archived_marker(self, item: dict) -> bool:
        for raw_key, value in item.items():
            key = normalize_name(raw_key)
            if key in ARCHIVED_BOOL_KEYS and bool(value):
                return True
            if key in ARCHIVED_STATUS_KEYS and self._contains_archived_marker(value):
                return True
            if key in ARCHIVED_TEXT_KEYS and self._contains_archived_marker(value):
                return True
        return False

    def _parse_visible_rating_tag(self, line: str, following_lines: Optional[list[str]] = None) -> tuple[str, str]:
        rating_match = VISIBLE_RATING_PATTERN.match(line or "")
        rating = rating_match.group(1).upper() if rating_match else "未知"
        lowered = (line or "").lower()
        if "trap" in lowered or "陷阱" in line:
            tag = "陷阱"
        elif "fun" in lowered or "娱乐" in line:
            tag = "娱乐"
        elif "bug" in lowered or "缺陷" in line:
            tag = "缺陷"
        else:
            tag = "强力联动"
        for candidate in following_lines or []:
            if candidate in SYNERGY_TAG_LABELS:
                tag = normalize_tag(candidate)
                break
            if candidate.isdigit() or candidate == "作者" or candidate.startswith(("作者：", "作者:")):
                break
        return rating, tag

    def _parse_visible_author_content(self, lines: list[str], start_index: int) -> tuple[str, str, bool, int, int]:
        author = "ApexLoL"
        is_original = False
        votes = []
        content_lines = []
        cursor = start_index
        while cursor < len(lines) and cursor < start_index + 12:
            candidate = lines[cursor]
            if candidate in SYNERGY_TAG_LABELS or candidate in {"原创", "非原创"}:
                is_original = is_original or candidate == "原创"
                cursor += 1
                continue
            if candidate.isdigit():
                votes.append(self._int_value(candidate))
                cursor += 1
                continue
            if candidate == "作者":
                next_line = lines[cursor + 1] if cursor + 1 < len(lines) else ""
                if next_line and not self._is_visible_noise_line(next_line):
                    author = next_line
                    cursor += 2
                    break
            if candidate.startswith(("作者：", "作者:")):
                author = candidate.split(":", 1)[-1].split("：", 1)[-1].strip() or author
                cursor += 1
                break
            cursor += 1

        while cursor < len(lines) and len(content_lines) < 12:
            candidate = lines[cursor]
            if (
                VISIBLE_RATING_PATTERN.match(candidate)
                or VISIBLE_STOP_LINE_PATTERN.match(candidate)
                or self._is_visible_noise_line(candidate)
            ):
                break
            if self._looks_like_augment_name(candidate, self._resolve_known_augment_names(candidate)):
                next_line = lines[cursor + 1] if cursor + 1 < len(lines) else ""
                if normalize_tier(next_line) != next_line or next_line in TIER_LABELS:
                    break
            if not candidate.startswith(("作者：", "作者:")):
                content_lines.append(candidate)
            cursor += 1

        upvotes = votes[0] if votes else 0
        downvotes = votes[1] if len(votes) > 1 else 0
        return author, _clean_text(" ".join(content_lines)), is_original, upvotes, downvotes

    @staticmethod
    def _is_visible_noise_line(candidate: str) -> bool:
        if not candidate:
            return True
        if candidate in {"+", "-", "推荐出装", "推荐召唤师技能"}:
            return True
        if re.match(r"^\d{4}年\d{1,2}月\d{1,2}日$", candidate):
            return True
        if re.match(r"^关联\s*\d+\s*个海克斯$", candidate):
            return True
        return False

    def _looks_like_augment_name(self, raw_name: str, resolved_names: list[str]) -> bool:
        text = str(raw_name or "").strip()
        if not text or not resolved_names:
            return False
        normalized = normalize_augment_name(text)
        resolved_tokens = {normalize_augment_name(name) for name in resolved_names}
        if normalized in resolved_tokens:
            return True
        return normalized in self.augment_name_map or text in self.augment_name_map

    def _looks_like_visible_augment_name(self, raw_name: str) -> bool:
        text = str(raw_name or "").strip()
        if not text or self._is_visible_noise_line(text):
            return False
        if text in SYNERGY_TAG_LABELS or text in TIER_LABELS or normalize_tier(text) != text:
            return False
        if VISIBLE_RATING_PATTERN.match(text) or text in {"作者", "原创", "非原创", "关联套装"}:
            return False
        if re.match(r"^关联\s*\d+\s*个海克斯$", text):
            return False
        return 1 < len(text) <= 40

    def _resolve_known_augment_names(self, raw_name: str) -> list[str]:
        key = str(raw_name or "").strip()
        if not key:
            return []
        candidates = [key, normalize_augment_name(key), Path(key).stem, normalize_augment_name(Path(key).stem)]
        for candidate in candidates:
            resolved = self.augment_name_map.get(candidate)
            if resolved:
                return [resolved]
        return []

    def _slug_from_visible_lines(self, lines: list[str]) -> str:
        head_text = " ".join(lines[:80])
        normalized_head = normalize_name(head_text)
        for key, champion in sorted(self.champion_lookup.items(), key=lambda item: len(item[0]), reverse=True):
            if not key or key.isdigit() or len(key) <= 1:
                continue
            if key.isascii() and len(key) < 3:
                continue
            if key in normalized_head:
                return champion.slug or normalize_slug(champion.en_name or champion.name)
        return ""

    def _extract_from_json_payload(self, payload: Any, fallback_slug: str) -> list[SynergyEntry]:
        entries = []
        for item, path in self._walk_json(payload):
            if not isinstance(item, dict):
                continue
            entry = self._entry_from_dict(item, fallback_slug=fallback_slug or self._slug_from_path(path))
            if entry:
                entries.append(entry)
        return entries

    def _walk_json(self, value: Any, path: tuple[str, ...] = ()):
        yield value, path
        if isinstance(value, dict):
            for key, child in value.items():
                yield from self._walk_json(child, (*path, str(key)))
        elif isinstance(value, list):
            for idx, child in enumerate(value):
                yield from self._walk_json(child, (*path, str(idx)))

    def _entry_from_dict(self, item: dict, fallback_slug: str) -> Optional[SynergyEntry]:
        if self._dict_has_archived_marker(item):
            self._record_archived_filter(
                reason="json_archived_marker",
                sample=json.dumps(item, ensure_ascii=False, sort_keys=True)[:500],
            )
            return None
        augment_names = self._resolve_augment_names(item)
        if not augment_names:
            return None
        content = self._resolve_content(item)
        if not content:
            return None
        champion_slug = self._resolve_champion_slug(item, fallback_slug=fallback_slug)
        if not champion_slug:
            return None
        return SynergyEntry(
            champion_slug=champion_slug,
            augment_names=augment_names,
            tier=normalize_tier(
                item.get("tier")
                or item.get("rarity")
                or item.get("rank")
                or item.get("augmentTier")
                or item.get("hextechTier")
            ),
            rating=self._resolve_rating(item),
            tag=normalize_tag(item.get("tags") or item.get("tag") or item.get("type")),
            author=str(item.get("author") or item.get("contributor") or item.get("user") or "ApexLoL").strip() or "ApexLoL",
            is_original=self._resolve_original_flag(item),
            content=content,
            upvotes=self._int_value(item.get("upvotes") or item.get("upVotes") or item.get("likes")),
            downvotes=self._int_value(item.get("downvotes") or item.get("downVotes") or item.get("dislikes")),
        )

    def _resolve_champion_slug(self, item: dict, fallback_slug: str) -> str:
        raw_values = [
            item.get("championSlug"),
            item.get("champion"),
            item.get("championName"),
            item.get("championId"),
            item.get("hero"),
            item.get("heroName"),
            item.get("champion_id"),
            item.get("champion_slug"),
            item.get("champion_name"),
            item.get("hero_id"),
            item.get("hero_name"),
            fallback_slug,
        ]
        for raw in raw_values:
            if raw is None:
                continue
            normalized = normalize_name(raw)
            slug = normalize_slug(raw)
            champion = self.champion_lookup.get(normalized) or self.champion_lookup.get(slug)
            if champion:
                return champion.slug or normalize_slug(champion.en_name or champion.name)
        return ""

    def _resolve_content(self, item: dict) -> str:
        note = (
            item.get("note")
            or item.get("content")
            or item.get("comment")
            or item.get("body")
            or item.get("guide")
            or item.get("description")
            or item.get("text")
            or item.get("tips")
        )
        if isinstance(note, dict):
            note = note.get("zh") or note.get("zh_CN") or note.get("cn") or note.get("en") or next(iter(note.values()), "")
        return _clean_text(note)

    def _resolve_augment_names(self, item: dict) -> list[str]:
        raw_values = []

        def append_raw(value: Any) -> None:
            if isinstance(value, list):
                for child in value:
                    append_raw(child)
            elif value is not None:
                raw_values.append(value)

        for key in (
            "hextechId",
            "hextechIds",
            "hextech",
            "hextechs",
            "hextechName",
            "hextechNames",
            "hextechSlug",
            "hextechSlugs",
            "augmentId",
            "augmentIds",
            "augment",
            "augments",
            "augmentName",
            "augmentNames",
            "augmentSlug",
            "augmentSlugs",
            "augment_name",
            "augment_names",
            "name",
        ):
            value = item.get(key)
            append_raw(value)

        names = []
        for raw in raw_values:
            if isinstance(raw, dict):
                raw = (
                    raw.get("name")
                    or raw.get("displayName")
                    or raw.get("display_name")
                    or raw.get("label")
                    or raw.get("id")
                    or raw.get("slug")
                )
            key = str(raw or "").strip()
            if not key:
                continue
            resolved = (
                self.augment_name_map.get(key)
                or self.augment_name_map.get(normalize_augment_name(key))
                or self.augment_name_map.get(Path(key).stem)
                or self.augment_name_map.get(normalize_augment_name(Path(key).stem))
            )
            if resolved:
                names.append(resolved)
            elif not key.isdigit() and len(key) > 1:
                names.append(key)
        return [name for name in dict.fromkeys(names) if name]

    def _resolve_rating(self, item: dict) -> str:
        value = item.get("rating") or item.get("grade") or item.get("score") or item.get("tierScore")
        if isinstance(value, dict):
            value = value.get("label") or value.get("grade") or value.get("rating") or value.get("value")
        text = str(value or "").strip()
        return text or "未知"

    @staticmethod
    def _resolve_original_flag(item: dict) -> bool:
        value = item.get("isOriginal")
        if value is None:
            value = item.get("original")
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "y", "原创", "original"}
        return bool(value)

    def _extract_old_bundle(self, bundle_text: str) -> list[SynergyEntry]:
        if BUNDLE_INTERACTION_SECTION_MARKER not in bundle_text:
            return []
        payload = self._extract_interaction_payload(bundle_text)
        arrays = payload["arrays"]
        entries = []
        for mapping_name in ("manual_map", "community_map"):
            champion_map = payload.get(mapping_name) or {}
            for champion_slug, short_key in champion_map.items():
                array_literal = arrays.get(str(short_key))
                if not array_literal:
                    continue
                try:
                    items = self._parse_js_array_literal(array_literal)
                except Exception as exc:
                    logger.warning("解析旧 bundle 数组失败：champion=%s error=%s", champion_slug, _safe_exception_label(exc))
                    continue
                for item in items:
                    entry = self._entry_from_dict(item, fallback_slug=str(champion_slug))
                    if entry:
                        entries.append(entry)
        return entries

    def _extract_generic_js_objects(self, text: str) -> list[SynergyEntry]:
        entries = []
        if "hextech" not in text and "augment" not in text and "rating" not in text:
            return entries
        object_pattern = re.compile(r"\{[^{}]{0,1200}(?:hextechId|hextechIds|augmentId|augmentIds|rating|isOriginal)[^{}]{0,1200}\}")
        for match in object_pattern.finditer(text):
            literal = match.group(0)
            try:
                item = ast.literal_eval(self._convert_js_literal_to_python(literal))
            except Exception:
                continue
            if isinstance(item, dict):
                entry = self._entry_from_dict(item, fallback_slug="")
                if entry:
                    entries.append(entry)
        return entries

    def _extract_interaction_payload(self, bundle_text: str) -> dict:
        section_index = bundle_text.find(BUNDLE_INTERACTION_SECTION_MARKER)
        if section_index == -1:
            raise ValueError("未找到联动数据起始标记")
        section_index += len(BUNDLE_INTERACTION_SECTION_MARKER)

        manual_index = bundle_text.find("Tk={", section_index)
        community_index = bundle_text.find("RA={", section_index)
        if manual_index == -1 or community_index == -1:
            raise ValueError("未找到英雄映射对象")

        manual_literal, manual_object_end = self._extract_js_object_literal(bundle_text, manual_index + len("Tk="))
        community_literal, _ = self._extract_js_object_literal(bundle_text, community_index + len("RA="))

        stop_index = bundle_text.rfind("],RA={", manual_object_end, community_index)
        stop_index = stop_index + 1 if stop_index != -1 else community_index
        short_key_arrays = {}
        short_key_arrays.update(self._extract_named_array_assignments(bundle_text, section_index, manual_index))
        short_key_arrays.update(self._extract_named_array_assignments(bundle_text, manual_object_end, stop_index))
        if not short_key_arrays:
            raise ValueError("未找到联动数组定义")

        return {
            "arrays": short_key_arrays,
            "manual_map": self._parse_js_identifier_map(manual_literal),
            "community_map": self._parse_js_identifier_map(community_literal),
        }

    def _extract_js_object_literal(self, text: str, start_index: int) -> tuple[str, int]:
        return self._extract_balanced_literal(text, start_index, "{", "}")

    def _extract_js_array_literal(self, text: str, start_index: int) -> tuple[str, int]:
        return self._extract_balanced_literal(text, start_index, "[", "]")

    def _extract_balanced_literal(self, text: str, start_index: int, opener: str, closer: str) -> tuple[str, int]:
        if start_index < 0 or start_index >= len(text) or text[start_index] != opener:
            raise ValueError("字面量起始位置无效")
        depth = 0
        quote = None
        escaped = False
        i = start_index
        while i < len(text):
            char = text[i]
            if quote:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = None
            else:
                if char in ('"', "'", "`"):
                    quote = char
                elif char == opener:
                    depth += 1
                elif char == closer:
                    depth -= 1
                    if depth == 0:
                        return text[start_index : i + 1], i + 1
            i += 1
        raise ValueError("字面量未闭合")

    def _extract_named_array_assignments(self, text: str, start_index: int, stop_index: int) -> dict:
        assignments = {}
        pattern = re.compile(r"([A-Za-z_$][A-Za-z0-9_$]*)=\[")
        cursor = start_index
        while cursor < stop_index:
            match = pattern.search(text, cursor, stop_index)
            if not match:
                break
            literal, cursor = self._extract_js_array_literal(text, match.end() - 1)
            assignments[match.group(1)] = literal
            if cursor < stop_index and text[cursor : cursor + 1] == ",":
                cursor += 1
        return assignments

    def _parse_js_identifier_map(self, literal: str) -> dict:
        body = literal.strip()
        if not body.startswith("{") or not body.endswith("}"):
            raise ValueError("英雄映射对象格式无效")
        mapping = {}
        pair_pattern = re.compile(r'''(?:"([^"]+)"|'([^']+)'|([A-Za-z_$][A-Za-z0-9_$]*))\s*:\s*([A-Za-z_$][A-Za-z0-9_$]*)''')
        for match in pair_pattern.finditer(body[1:-1]):
            key = match.group(1) or match.group(2) or match.group(3)
            value = match.group(4)
            if key:
                mapping[key] = value
        if not mapping:
            raise ValueError("英雄映射对象解析为空")
        return mapping

    def _parse_js_array_literal(self, literal: str) -> list:
        return ast.literal_eval(self._convert_js_literal_to_python(literal))

    def _convert_js_literal_to_python(self, literal: str) -> str:
        result = []
        i = 0
        simple_escapes = {"n": "\\n", "r": "\\r", "t": "\\t", "b": "\\b", "f": "\\f", "\\": "\\", '"': '"', "'": "'", "`": "`", "/": "/"}
        while i < len(literal):
            char = literal[i]
            if char in ('"', "'", "`"):
                quote = char
                i += 1
                chunks = []
                while i < len(literal):
                    current = literal[i]
                    if current == "\\":
                        i += 1
                        if i >= len(literal):
                            chunks.append("\\")
                            break
                        escaped = literal[i]
                        if escaped == "u" and i + 4 < len(literal):
                            hex_part = literal[i + 1 : i + 5]
                            if all(c in "0123456789abcdefABCDEF" for c in hex_part):
                                chunks.append(chr(int(hex_part, 16)))
                                i += 5
                                continue
                        chunks.append(simple_escapes.get(escaped, escaped))
                        i += 1
                        continue
                    if current == quote:
                        i += 1
                        break
                    chunks.append(current)
                    i += 1
                result.append(json.dumps("".join(chunks), ensure_ascii=False))
                continue
            if char == "!" and i + 1 < len(literal) and literal[i + 1] in "01":
                result.append("True" if literal[i + 1] == "0" else "False")
                i += 2
                continue
            if char.isalpha() or char in "_$":
                j = i + 1
                while j < len(literal) and (literal[j].isalnum() or literal[j] in "_$"):
                    j += 1
                token = literal[i:j]
                k = j
                while k < len(literal) and literal[k].isspace():
                    k += 1
                if k < len(literal) and literal[k] == ":":
                    result.append(json.dumps(token, ensure_ascii=False))
                    result.append(literal[j : k + 1])
                    i = k + 1
                    continue
                result.append({"null": "None", "true": "True", "false": "False", "undefined": "None"}.get(token, token))
                i = j
                continue
            result.append(char)
            i += 1
        return "".join(result)

    def _slug_from_url(self, url: str) -> str:
        path = urlparse(url).path.rstrip("/")
        tail = path.rsplit("/", 1)[-1]
        return normalize_slug(Path(tail).stem or tail)

    def _slug_from_path(self, path: tuple[str, ...]) -> str:
        for part in reversed(path):
            normalized = normalize_name(part)
            slug = normalize_slug(part)
            if normalized in self.champion_lookup:
                return normalized
            if slug in self.champion_lookup:
                return slug
        return ""

    @staticmethod
    def _int_value(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _dedupe_entries(entries_by_slug: dict[str, list[SynergyEntry]]) -> dict[str, list[SynergyEntry]]:
        result = {}
        for slug, entries in entries_by_slug.items():
            seen = set()
            unique = []
            for entry in entries:
                key = (tuple(entry.augment_names), entry.rating, entry.tag, entry.author, entry.content)
                if key in seen:
                    continue
                seen.add(key)
                unique.append(entry)
            result[slug] = unique
        return result
