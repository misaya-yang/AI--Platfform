"""
Multimodal Reranker using Vision-Language Models.

Part of P3: Multimodal RAG Full-Chain Optimization

Uses DashScope VLM (qwen-vl-max) to score image-query relevance,
enabling cross-modal reranking for multimodal RAG.

Strategy:
1. For image candidates: Use VLM to evaluate relevance to text query
2. For text candidates with images: Consider associated images in scoring
3. Combine multimodal scores with original retrieval scores
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .vlm_service import DashScopeVLMService

logger = logging.getLogger(__name__)


@dataclass
class RerankCandidate:
    """Candidate for multimodal reranking."""
    segment_id: str
    text: Optional[str] = None
    image_url: Optional[str] = None
    image_bytes: Optional[bytes] = None
    media_type: str = "text"  # "text" | "image"
    original_score: float = 0.0
    rerank_score: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)


class MultimodalReranker:
    """
    Multimodal reranker using Vision-Language Models.

    For image candidates, uses VLM to evaluate query relevance:
    - Generate relevance assessment between image and query
    - Output a score [0, 1] indicating how well the image answers the query

    For text candidates with associated images:
    - Optionally boost scores if images are highly relevant
    - Use VLM to verify image-text coherence

    Configuration:
        vlm_service: DashScopeVLMService instance for image analysis
        max_concurrent: Maximum concurrent VLM calls
        timeout_seconds: Timeout for each VLM call
        image_weight: How much to weight image scores vs original scores
    """

    # Prompt for evaluating image-query relevance
    RELEVANCE_PROMPT = '''你是一个相关性评估专家。请评估以下图片与用户查询的相关性。

用户查询: {query}

请仔细分析图片内容，然后给出相关性评分:
- 如果图片直接回答了用户问题，评分 0.9-1.0
- 如果图片部分相关，评分 0.6-0.8
- 如果图片略微相关，评分 0.3-0.5
- 如果图片完全不相关，评分 0.0-0.2

请只输出一个0到1之间的数字作为评分，不要输出其他内容。'''

    # Alternative English prompt
    RELEVANCE_PROMPT_EN = '''You are a relevance assessment expert. Evaluate how relevant this image is to the user's query.

User Query: {query}

Analyze the image content carefully, then provide a relevance score:
- If the image directly answers the question: 0.9-1.0
- If the image is partially relevant: 0.6-0.8
- If the image is slightly relevant: 0.3-0.5
- If the image is not relevant: 0.0-0.2

Output ONLY a single number between 0 and 1 as the score, nothing else.'''

    def __init__(
        self,
        vlm_service: Optional["DashScopeVLMService"] = None,
        max_concurrent: int = 3,
        timeout_seconds: float = 30.0,
        image_weight: float = 0.4,
        use_english_prompt: bool = False,
    ):
        """
        Initialize MultimodalReranker.

        Args:
            vlm_service: VLM service for image analysis. If None, image scoring is skipped.
            max_concurrent: Maximum concurrent VLM calls (default 3)
            timeout_seconds: Timeout per VLM call (default 30s)
            image_weight: Weight for image score when combining with original (default 0.4)
            use_english_prompt: Use English prompt instead of Chinese (default False)
        """
        self.vlm_service = vlm_service
        self.max_concurrent = max_concurrent
        self.timeout = timeout_seconds
        self.image_weight = image_weight
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self.prompt_template = self.RELEVANCE_PROMPT_EN if use_english_prompt else self.RELEVANCE_PROMPT

    async def rerank(
        self,
        query: str,
        candidates: List[RerankCandidate],
        top_k: int = 10,
        rerank_images_only: bool = False,
        score_threshold: float = 0.0,
    ) -> List[RerankCandidate]:
        """
        Rerank candidates using multimodal scoring.

        Args:
            query: User query text
            candidates: List of candidates to rerank
            top_k: Return top-k results after reranking
            rerank_images_only: If True, only rerank image candidates (text keeps original score)
            score_threshold: Minimum score to include in results

        Returns:
            Reranked candidates sorted by score (descending)
        """
        if not candidates:
            return []

        # Separate image and text candidates
        image_candidates = [c for c in candidates if c.media_type == "image"]
        text_candidates = [c for c in candidates if c.media_type == "text"]

        logger.info(f"Multimodal rerank: {len(image_candidates)} images, {len(text_candidates)} text candidates")

        # Score image candidates using VLM
        if image_candidates and self.vlm_service:
            await self._score_images_batch(query, image_candidates)

        # Text candidates keep original score (or can be boosted by associated images)
        for c in text_candidates:
            if rerank_images_only:
                c.rerank_score = c.original_score
            else:
                # Optionally enhance with VLM-based scoring in future
                c.rerank_score = c.original_score

        # Combine and sort
        all_candidates = image_candidates + text_candidates
        all_candidates = [c for c in all_candidates if c.rerank_score >= score_threshold]
        all_candidates.sort(key=lambda x: x.rerank_score, reverse=True)

        return all_candidates[:top_k]

    async def _score_images_batch(
        self,
        query: str,
        candidates: List[RerankCandidate],
    ) -> None:
        """
        Score image candidates in batch with concurrency control.

        Args:
            query: User query
            candidates: Image candidates to score (modified in-place)
        """
        async def score_one(c: RerankCandidate):
            async with self._semaphore:
                try:
                    vlm_score = await self._score_single_image(query, c)
                    # Combine VLM score with original score
                    c.rerank_score = (
                        (1 - self.image_weight) * c.original_score +
                        self.image_weight * vlm_score
                    )
                except asyncio.TimeoutError:
                    logger.warning(f"Timeout scoring image {c.segment_id}")
                    c.rerank_score = c.original_score * 0.7  # Penalize timeout
                except Exception as e:
                    logger.warning(f"Failed to score image {c.segment_id}: {e}")
                    c.rerank_score = c.original_score * 0.5  # Penalize errors

        tasks = [score_one(c) for c in candidates]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _score_single_image(
        self,
        query: str,
        candidate: RerankCandidate,
    ) -> float:
        """
        Score a single image against the query using VLM.

        Args:
            query: User query
            candidate: Image candidate

        Returns:
            Relevance score [0.0, 1.0]
        """
        if not self.vlm_service:
            return candidate.original_score

        # Get image bytes
        image_bytes = candidate.image_bytes
        if not image_bytes and candidate.image_url:
            image_bytes = await self._load_image_from_url(candidate.image_url)

        if not image_bytes:
            logger.warning(f"No image data for {candidate.segment_id}")
            return candidate.original_score

        # Build prompt
        prompt = self.prompt_template.format(query=query)

        try:
            # Call VLM with timeout
            result = await asyncio.wait_for(
                self.vlm_service.describe_image(
                    image_bytes=image_bytes,
                    prompt=prompt,
                    max_tokens=10,  # Only need a number
                ),
                timeout=self.timeout,
            )

            # Parse score from response
            score_text = result.description.strip()

            # Try to extract number from response
            score = self._parse_score(score_text)
            logger.debug(f"VLM scored {candidate.segment_id}: {score} (raw: {score_text[:50]})")
            return score

        except Exception as e:
            logger.warning(f"VLM scoring failed for {candidate.segment_id}: {e}")
            return candidate.original_score

    def _parse_score(self, text: str) -> float:
        """
        Parse a score from VLM response text.

        Handles various formats:
        - "0.85"
        - "Score: 0.85"
        - "The relevance score is 0.85"
        - "85%"

        Args:
            text: VLM response text

        Returns:
            Score in range [0.0, 1.0]
        """
        import re

        text = text.strip()

        # Try direct float parsing
        try:
            score = float(text)
            if 0 <= score <= 1:
                return score
            elif 0 <= score <= 100:
                return score / 100.0
        except ValueError:
            pass

        # Try to extract number from text
        # Match patterns like "0.85", ".85", "85%", "85"
        patterns = [
            r'(\d+\.\d+)',  # 0.85
            r'\.(\d+)',     # .85
            r'(\d+)%',      # 85%
            r'(\d+)',       # 85
        ]

        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                try:
                    value = float(match.group(1))
                    if pattern == r'\.(\d+)':
                        value = float(f"0.{match.group(1)}")
                    if value > 1 and value <= 100:
                        value = value / 100.0
                    if 0 <= value <= 1:
                        return value
                except ValueError:
                    continue

        # Default to 0.5 if parsing fails
        logger.warning(f"Could not parse score from: {text[:100]}")
        return 0.5

    async def _load_image_from_url(self, url: str) -> Optional[bytes]:
        """
        Load image bytes from URL.

        Args:
            url: Image URL (S3, OSS, or HTTP)

        Returns:
            Image bytes or None if loading fails
        """
        import httpx

        if not url:
            return None

        try:
            # Handle S3/OSS URLs
            if url.startswith("s3://") or url.startswith("oss://"):
                # TODO: Implement S3/OSS loading via storage service
                logger.warning(f"S3/OSS URL loading not implemented: {url}")
                return None

            # HTTP(S) URL
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url)
                response.raise_for_status()
                return response.content

        except Exception as e:
            logger.warning(f"Failed to load image from {url}: {e}")
            return None

    async def score_text_with_images(
        self,
        query: str,
        text: str,
        image_urls: List[str],
        original_score: float = 0.0,
    ) -> float:
        """
        Score a text segment by also considering its associated images.

        This can boost the score of text segments that have highly
        relevant associated images.

        Args:
            query: User query
            text: Text content
            image_urls: URLs of associated images
            original_score: Original retrieval score

        Returns:
            Enhanced score incorporating image relevance
        """
        if not image_urls or not self.vlm_service:
            return original_score

        # Score each associated image
        image_scores: List[float] = []
        for url in image_urls[:3]:  # Limit to top 3 images
            try:
                image_bytes = await self._load_image_from_url(url)
                if image_bytes:
                    candidate = RerankCandidate(
                        segment_id="temp",
                        image_url=url,
                        image_bytes=image_bytes,
                        media_type="image",
                        original_score=0.5,
                    )
                    score = await self._score_single_image(query, candidate)
                    image_scores.append(score)
            except Exception as e:
                logger.warning(f"Failed to score associated image {url}: {e}")

        if not image_scores:
            return original_score

        # Average image score
        avg_image_score = sum(image_scores) / len(image_scores)

        # Combine: if images are relevant, boost the text score
        if avg_image_score > 0.6:
            # Images are relevant - boost text score
            boost = 0.1 * (avg_image_score - 0.5)  # Max boost of 0.05 at score 1.0
            return min(1.0, original_score + boost)

        return original_score


def create_multimodal_reranker(
    vlm_service: Optional["DashScopeVLMService"] = None,
    max_concurrent: int = 3,
    timeout_seconds: float = 30.0,
) -> MultimodalReranker:
    """
    Factory function to create a MultimodalReranker.

    Args:
        vlm_service: VLM service for image analysis
        max_concurrent: Maximum concurrent VLM calls
        timeout_seconds: Timeout per VLM call

    Returns:
        Configured MultimodalReranker instance
    """
    return MultimodalReranker(
        vlm_service=vlm_service,
        max_concurrent=max_concurrent,
        timeout_seconds=timeout_seconds,
    )
