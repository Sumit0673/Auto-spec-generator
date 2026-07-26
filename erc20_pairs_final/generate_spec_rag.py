#!/usr/bin/env python3
"""
generate_spec_rag.py
────────────────────
A Retrieval-Augmented Generation (RAG) pipeline to automatically generate 
Certora Verification Language (CVL) specs for new Solidity contracts.

Usage:
    python3 generate_spec_rag.py --contract src/MyToken.sol --query "ERC20 transfer rules"

This script will:
1. Embed the search query (or contract source).
2. Retrieve the most relevant CVL specs from the local Chroma DB.
3. Feed the retrieved examples + the target contract to an LLM.
4. Output the generated .spec file.
"""

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

# Import the existing query logic from our vector store setup
from query_specs import query as retrieve_specs

load_dotenv()  # Load environment variables from .env

# Configure your LLM here. We are using NVIDIA NIM.
LLM_MODEL = os.getenv("LLM_MODEL", "meta/llama-3.1-70b-instruct")

def generate_spec(contract_code: str, retrieved_context: list[dict], model: str = LLM_MODEL) -> str:
    """Generate a CVL spec using the retrieved context as examples."""
    
    # 1. Build the context string from the retrieved vector chunks
    context_str = ""
    for idx, chunk in enumerate(retrieved_context, 1):
        context_str += f"--- Example {idx} (from {chunk['contract_name']} - {chunk['spec_filename']}) ---\n"
        context_str += chunk['document'] + "\n\n"

    # 2. Construct the prompt
    system_prompt = (
        "You are an expert formal verification engineer specializing in Certora Verification Language (CVL). "
        "Your task is to write a highly accurate and comprehensive `.spec` file for a provided Solidity contract.\n\n"
        "Instructions:\n"
        "1. Analyze the provided target Solidity contract.\n"
        "2. Review the provided 'Verified CVL Examples' for syntax, structure, and relevant rules (e.g. transfer fees, authorization, invariants).\n"
        "3. Write a full, syntactically correct CVL specification tailored to the target contract.\n"
        "4. Include necessary `methods` blocks and `ghost`/`hook` declarations if needed.\n"
        "5. Output ONLY the raw CVL code. Do not include markdown formatting like ```cvl or ```solidity, just the raw text."
    )

    user_prompt = f"""
### Verified CVL Examples (Reference Material)
{context_str}

### Target Solidity Contract
```solidity
{contract_code}
```

Based on the target contract above, generate the complete CVL specification.
"""

    print(f"Calling LLM ({model}) to generate the spec. This may take a moment...")
    
    # 3. Call the LLM
    try:
        client = OpenAI(
            base_url="https://integrate.api.nvidia.com/v1",
            api_key=os.environ.get("NVDIA_API_KEY")
        )
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.2, # Low temperature for more deterministic/technical output
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"Error calling LLM: {e}")
        print("Please ensure your NVDIA_API_KEY environment variable is set.")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Generate CVL specs using RAG.")
    parser.add_argument("--contract", required=True, help="Path to the target Solidity contract.")
    parser.add_argument("--query", help="Optional: Describe what rules you want (e.g. 'transfer invariants'). Defaults to using contract source as query.")
    parser.add_argument("--top_k", type=int, default=3, help="Number of example specs to retrieve (default: 3).")
    parser.add_argument("--out", help="Optional: Path to save the generated .spec file.")
    
    args = parser.parse_args()
    
    contract_path = Path(args.contract)
    if not contract_path.exists():
        print(f"Error: Contract file '{args.contract}' not found.")
        sys.exit(1)
        
    contract_code = contract_path.read_text(encoding="utf-8")
    
    # Use the provided query, or fallback to the contract code (truncated to avoid huge embedding queries)
    search_query = args.query if args.query else contract_code[:4000]
    
    print(f"Retrieving top {args.top_k} similar specs from vector database...")
    results, elapsed = retrieve_specs(search_query, top_k=args.top_k)
    print(f"Found {len(results)} relevant chunks in {elapsed*1000:.0f}ms.")
    
    # Generate the spec
    generated_spec = generate_spec(contract_code, results)
    
    # Remove markdown code blocks if the LLM added them despite instructions
    if generated_spec.startswith("```"):
        lines = generated_spec.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines[-1].startswith("```"):
            lines = lines[:-1]
        generated_spec = "\n".join(lines)
    
    if not args.out:
        args.out = contract_path.stem + ".spec"
        
    out_path = Path(args.out)
    out_path.write_text(generated_spec, encoding="utf-8")
    print(f"\nSuccess! Generated spec saved to {out_path}")


if __name__ == "__main__":
    main()
