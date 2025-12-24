"""
LangGraph-compatible retrieval tool interface.

Provides tools that can be used directly by LangGraph agents
for knowledge base retrieval operations.

Compatible with both LangChain tools format and direct function calls.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Union

from ...core.auth.user_resolver import UserContext
from .knowledge_service import KnowledgeService, RetrieveResult


@dataclass
class KBRetrievalInput:
    """Input schema for knowledge base retrieval."""
    query: str
    dataset_id: str
    top_k: int = 5
    mode: str = "hybrid"  # hybrid | vector | keyword
    document_id: Optional[str] = None
    rerank: bool = False
    mmr: bool = False


@dataclass
class KBRetrievalOutput:
    """Output schema for knowledge base retrieval."""
    segments: List[Dict[str, Any]]
    metadata: Dict[str, Any]
    query: str
    dataset_id: str


@dataclass
class KBSearchResult:
    """A single search result for LangGraph consumption."""
    content: str
    score: float
    segment_id: str
    document_id: str
    metadata: Dict[str, Any]
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "content": self.content,
            "score": self.score,
            "segment_id": self.segment_id,
            "document_id": self.document_id,
            "metadata": self.metadata,
        }


class KnowledgeRetriever:
    """
    Knowledge base retriever for LangGraph agents.
    
    This class provides multiple interfaces for knowledge retrieval:
    - Direct async method calls
    - LangChain tool-compatible interface
    - Structured input/output for type safety
    
    Example usage in LangGraph:
    
    ```python
    # Create retriever
    retriever = KnowledgeRetriever(kb_service, dataset_id="my_dataset")
    
    # Use as a tool in LangGraph
    tools = [retriever.as_tool()]
    
    # Or call directly
    results = await retriever.retrieve("What is X?")
    ```
    """
    
    def __init__(
        self,
        knowledge_service: KnowledgeService,
        dataset_id: str,
        user_context: Optional[UserContext] = None,
        default_top_k: int = 5,
        default_mode: str = "hybrid",
        default_rerank: bool = False,
        default_mmr: bool = False,
    ):
        """
        Initialize the retriever.
        
        Args:
            knowledge_service: The knowledge base service
            dataset_id: Default dataset to search
            user_context: User context for authorization (uses system if None)
            default_top_k: Default number of results to return
            default_mode: Default retrieval mode
            default_rerank: Whether to use reranking by default
            default_mmr: Whether to use MMR by default
        """
        self.kb = knowledge_service
        self.dataset_id = dataset_id
        self.user_context = user_context or self._create_system_context()
        self.default_top_k = default_top_k
        self.default_mode = default_mode
        self.default_rerank = default_rerank
        self.default_mmr = default_mmr
    
    @staticmethod
    def _create_system_context() -> UserContext:
        """Create a system-level user context for internal use."""
        return UserContext(
            user_id="system",
            tenant_id="system",
            is_authenticated=True,
            tier="admin",
            roles=["admin"],
        )
    
    async def retrieve(
        self,
        query: str,
        top_k: Optional[int] = None,
        mode: Optional[str] = None,
        document_id: Optional[str] = None,
        rerank: Optional[bool] = None,
        mmr: Optional[bool] = None,
        dataset_id: Optional[str] = None,
        **kwargs,
    ) -> List[KBSearchResult]:
        """
        Retrieve relevant segments from the knowledge base.
        
        Args:
            query: The search query
            top_k: Number of results to return
            mode: Retrieval mode (hybrid/vector/keyword)
            document_id: Optional filter to specific document
            rerank: Whether to use reranking
            mmr: Whether to use MMR diversity
            dataset_id: Override the default dataset
            **kwargs: Additional parameters passed to KB retrieve
        
        Returns:
            List of KBSearchResult objects
        """
        results, meta = await self.kb.retrieve(
            user=self.user_context,
            dataset_id=dataset_id or self.dataset_id,
            query=query,
            top_k=top_k or self.default_top_k,
            mode=mode or self.default_mode,
            document_id=document_id,
            rerank=rerank if rerank is not None else self.default_rerank,
            mmr=mmr if mmr is not None else self.default_mmr,
            **kwargs,
        )
        
        return [
            KBSearchResult(
                content=r.text,
                score=r.score,
                segment_id=r.segment_id,
                document_id=r.document_id,
                metadata=r.metadata,
            )
            for r in results
        ]
    
    async def retrieve_texts(
        self,
        query: str,
        top_k: Optional[int] = None,
        **kwargs,
    ) -> List[str]:
        """
        Retrieve relevant text segments as a simple list of strings.
        
        This is a convenience method for simple RAG pipelines.
        """
        results = await self.retrieve(query, top_k=top_k, **kwargs)
        return [r.content for r in results]
    
    async def retrieve_with_metadata(
        self,
        query: str,
        top_k: Optional[int] = None,
        **kwargs,
    ) -> KBRetrievalOutput:
        """
        Retrieve with full metadata including retrieval statistics.
        """
        results, meta = await self.kb.retrieve(
            user=self.user_context,
            dataset_id=self.dataset_id,
            query=query,
            top_k=top_k or self.default_top_k,
            mode=self.default_mode,
            rerank=self.default_rerank,
            mmr=self.default_mmr,
            **kwargs,
        )
        
        return KBRetrievalOutput(
            segments=[
                {
                    "content": r.text,
                    "score": r.score,
                    "segment_id": r.segment_id,
                    "document_id": r.document_id,
                    "metadata": r.metadata,
                }
                for r in results
            ],
            metadata=meta,
            query=query,
            dataset_id=self.dataset_id,
        )
    
    def as_langchain_tool(self) -> Dict[str, Any]:
        """
        Return a LangChain-compatible tool definition.
        
        This can be used with LangChain's tool decorator or added to a toolkit.
        
        Note: This returns a sync wrapper that should be used in async context.
        """
        import asyncio
        
        async def _retrieve(query: str, top_k: int = 5) -> str:
            """Retrieve relevant information from the knowledge base."""
            results = await self.retrieve(query, top_k=top_k)
            
            if not results:
                return "No relevant information found."
            
            # Format results as text
            formatted = []
            for i, r in enumerate(results, 1):
                formatted.append(f"[{i}] {r.content}")
            
            return "\n\n".join(formatted)
        
        # Return tool definition
        return {
            "name": f"kb_search_{self.dataset_id}",
            "description": f"Search the knowledge base '{self.dataset_id}' for relevant information. Use this tool to find answers to questions or gather context.",
            "func": _retrieve,
            "coroutine": _retrieve,
            "args_schema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query to find relevant information"
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Number of results to return",
                        "default": 5
                    }
                },
                "required": ["query"]
            }
        }
    
    def as_openai_function(self) -> Dict[str, Any]:
        """
        Return an OpenAI function calling compatible definition.
        """
        return {
            "type": "function",
            "function": {
                "name": f"search_knowledge_base",
                "description": f"Search the knowledge base for relevant information to answer questions. Dataset: {self.dataset_id}",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The search query"
                        },
                        "top_k": {
                            "type": "integer",
                            "description": "Number of results to return",
                            "default": 5
                        }
                    },
                    "required": ["query"]
                }
            }
        }


class MultiDatasetRetriever:
    """
    Retriever that can search across multiple datasets.
    
    Useful for LangGraph agents that need to access multiple knowledge bases.
    """
    
    def __init__(
        self,
        knowledge_service: KnowledgeService,
        dataset_ids: List[str],
        user_context: Optional[UserContext] = None,
        default_top_k: int = 5,
        default_mode: str = "hybrid",
    ):
        self.kb = knowledge_service
        self.dataset_ids = dataset_ids
        self.user_context = user_context or KnowledgeRetriever._create_system_context()
        self.default_top_k = default_top_k
        self.default_mode = default_mode
        
        # Create individual retrievers
        self.retrievers = {
            ds_id: KnowledgeRetriever(
                knowledge_service=knowledge_service,
                dataset_id=ds_id,
                user_context=self.user_context,
                default_top_k=default_top_k,
                default_mode=default_mode,
            )
            for ds_id in dataset_ids
        }
    
    async def retrieve(
        self,
        query: str,
        top_k: Optional[int] = None,
        dataset_id: Optional[str] = None,
        merge_results: bool = True,
        **kwargs,
    ) -> List[KBSearchResult]:
        """
        Retrieve from one or all datasets.
        
        Args:
            query: The search query
            top_k: Number of results to return
            dataset_id: Specific dataset to search (if None, search all)
            merge_results: If searching all, whether to merge and rank results
            **kwargs: Additional parameters
        
        Returns:
            List of search results
        """
        top_k = top_k or self.default_top_k
        
        if dataset_id:
            # Search single dataset
            if dataset_id not in self.retrievers:
                raise ValueError(f"Dataset {dataset_id} not configured")
            return await self.retrievers[dataset_id].retrieve(query, top_k=top_k, **kwargs)
        
        # Search all datasets
        import asyncio
        
        tasks = [
            retriever.retrieve(query, top_k=top_k, **kwargs)
            for retriever in self.retrievers.values()
        ]
        
        all_results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Flatten results
        merged: List[KBSearchResult] = []
        for result_list in all_results:
            if isinstance(result_list, list):
                merged.extend(result_list)
        
        if merge_results and merged:
            # Sort by score and take top_k
            merged.sort(key=lambda x: x.score, reverse=True)
            merged = merged[:top_k]
        
        return merged
    
    def as_openai_function(self) -> Dict[str, Any]:
        """Return OpenAI function calling definition."""
        dataset_desc = ", ".join(self.dataset_ids)
        
        return {
            "type": "function",
            "function": {
                "name": "search_knowledge_bases",
                "description": f"Search knowledge bases for relevant information. Available datasets: {dataset_desc}",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The search query"
                        },
                        "dataset_id": {
                            "type": "string",
                            "description": f"Optional: specific dataset to search. Options: {dataset_desc}. If not provided, searches all datasets.",
                            "enum": self.dataset_ids
                        },
                        "top_k": {
                            "type": "integer",
                            "description": "Number of results to return",
                            "default": 5
                        }
                    },
                    "required": ["query"]
                }
            }
        }


def create_retrieval_tool(
    knowledge_service: KnowledgeService,
    dataset_id: str,
    user_context: Optional[UserContext] = None,
    **kwargs,
) -> Callable:
    """
    Create a simple retrieval tool function for LangGraph.
    
    Usage in LangGraph:
    ```python
    search_tool = create_retrieval_tool(kb_service, "my_dataset")
    
    # In your graph node
    results = await search_tool("What is X?")
    ```
    """
    retriever = KnowledgeRetriever(
        knowledge_service=knowledge_service,
        dataset_id=dataset_id,
        user_context=user_context,
        **kwargs,
    )
    
    async def search(query: str, top_k: int = 5) -> str:
        """Search the knowledge base for relevant information."""
        results = await retriever.retrieve(query, top_k=top_k)
        
        if not results:
            return "No relevant information found in the knowledge base."
        
        # Format as numbered list
        formatted = []
        for i, r in enumerate(results, 1):
            formatted.append(f"[{i}] (score: {r.score:.3f})\n{r.content}")
        
        return "\n\n---\n\n".join(formatted)
    
    return search


# Dify-compatible external knowledge base interface

@dataclass
class DifyExternalDatasetConfig:
    """Configuration for Dify external dataset API."""
    dataset_id: str
    retrieval_model: Dict[str, Any]
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "retrieval_model": self.retrieval_model,
        }


class DifyCompatibleKBAPI:
    """
    Provides a Dify-compatible API interface for the knowledge base.
    
    This allows the KB to be used as an external knowledge base in Dify applications.
    """
    
    def __init__(
        self,
        knowledge_service: KnowledgeService,
        default_user_context: Optional[UserContext] = None,
    ):
        self.kb = knowledge_service
        self.user_context = default_user_context or KnowledgeRetriever._create_system_context()
    
    async def retrieve(
        self,
        dataset_id: str,
        query: str,
        retrieval_model: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Dify-compatible retrieval endpoint.
        
        Returns results in Dify's expected format.
        """
        # Parse retrieval model config
        model = retrieval_model or {}
        
        top_k = int(model.get("top_k", 5))
        score_threshold = model.get("score_threshold")
        rerank = model.get("reranking_enable", False)
        
        # Map Dify search method to our mode
        search_method = str(model.get("search_method", "hybrid_search")).lower()
        mode_mapping = {
            "semantic_search": "vector",
            "full_text_search": "keyword",
            "hybrid_search": "hybrid",
        }
        mode = mode_mapping.get(search_method, "hybrid")
        
        # Execute retrieval
        results, meta = await self.kb.retrieve(
            user=self.user_context,
            dataset_id=dataset_id,
            query=query,
            top_k=top_k,
            mode=mode,
            rerank=rerank,
        )
        
        # Filter by score threshold
        if score_threshold is not None:
            results = [r for r in results if r.score >= score_threshold]
        
        # Format as Dify response
        return {
            "records": [
                {
                    "segment": {
                        "id": r.segment_id,
                        "document_id": r.document_id,
                        "content": r.text,
                        "position": r.metadata.get("position", 0),
                        "word_count": len(r.text.split()),
                        "tokens": r.metadata.get("token_count", 0),
                        "keywords": r.metadata.get("keywords", []),
                        "hit_count": r.metadata.get("hit_count", 0),
                    },
                    "score": r.score,
                    "tsne_position": None,  # Not implemented
                }
                for r in results
            ],
            "metadata": {
                "total": len(results),
                "query": query,
                "dataset_id": dataset_id,
            },
        }

