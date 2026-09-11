"""
Solidity Function Graph Analyzer

Analyzes Solidity contracts using slither-analyzer to extract:
- Function call graphs (intra-contract and cross-contract)
- State variable read/write patterns
- Modifiers and their usage
- Events and their emission points
- Inheritance hierarchies
- ERC interface detection

Exports to:
- JSON (structured function graph with metadata)
- GraphML (for Gephi/Cytoscape visualization)
- RAG chunks (for Chroma vector DB embedding)
"""

__version__ = "0.1.0"

from solidity_graph.analyzer import SolidityAnalyzer, analyze_path
from solidity_graph.rag_exporter import export_rag_chunks

__all__ = [
    "SolidityAnalyzer",
    "analyze_path",
    "export_rag_chunks",
]