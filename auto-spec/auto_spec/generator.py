"""
Main specification generator using RAG + LLM.
"""

import os
from pathlib import Path
from typing import Optional

from openai import OpenAI

from auto_spec.config import get_config
from auto_spec.vector_db import VectorDBManager
from auto_spec.prompts import format_property_gpt_prompt


class SpecGenerator:
    """Generate CVL specifications for Solidity contracts."""
    
    def __init__(self, config=None):
        self.config = config or get_config()
        self.vector_db = VectorDBManager(self.config)
        self._validate_config()
    
    def _validate_config(self):
        """Validate configuration."""
        is_valid, error_msg = self.config.validate()
        if not is_valid:
            raise RuntimeError(f"Configuration error: {error_msg}")
    
    def generate(
        self,
        contract_path: str,
        query: Optional[str] = None,
        top_k: Optional[int] = None,
        output_path: Optional[str] = None
    ) -> str:
        """
        Generate CVL specification for a Solidity contract.
        
        Args:
            contract_path: Path to Solidity contract file
            query: Search query (defaults to contract source)
            top_k: Number of reference specs to retrieve
            output_path: Path to save generated spec file
            
        Returns:
            str: Generated CVL specification
        """
        contract_path = Path(contract_path)
        if not contract_path.exists():
            raise FileNotFoundError(f"Contract file not found: {contract_path}")
        
        # Read contract code
        contract_code = contract_path.read_text()
        contract_name = contract_path.stem
        
        # Use contract code as query if not provided
        query = query or contract_code[:500]
        
        # Retrieve reference specs
        print(f"Retrieving similar specs for: {contract_name}...")
        retrieved_context = self.vector_db.query(query, top_k=top_k)
        
        if not retrieved_context:
            print("Warning: No similar specs found in database")
        else:
            print(f"Found {len(retrieved_context)} reference specs")
        
        # Generate spec using LLM
        print(f"Generating CVL spec using {self.config.LLM_MODEL}...")
        spec_content = self._call_llm(
            contract_code=contract_code,
            retrieved_context=retrieved_context,
            contract_name=contract_name
        )
        
        # Save spec file if requested
        if output_path or self.config.SAVE_SPEC_FILE:
            save_path = output_path or self.config.OUTPUT_DIR / f"{contract_name}.spec"
            save_path = Path(save_path)
            save_path.parent.mkdir(parents=True, exist_ok=True)
            save_path.write_text(spec_content)
            print(f"✓ Spec saved to: {save_path}")
        
        return spec_content
    
    def _call_llm(
        self,
        contract_code: str,
        retrieved_context: list,
        contract_name: str
    ) -> str:
        """Call LLM with formatted prompt.
        
        Args:
            contract_code: Solidity source code
            retrieved_context: List of reference specs
            contract_name: Name of target contract
            
        Returns:
            str: Generated specification
        """
        system_prompt, user_prompt = format_property_gpt_prompt(
            contract_code=contract_code,
            retrieved_context=retrieved_context,
            contract_name=contract_name
        )
        
        try:
            # Determine API endpoint based on provider
            if self.config.LLM_PROVIDER == "nvidia":
                client = OpenAI(
                    base_url=self.config.LLM_BASE_URL,
                    api_key=self.config.LLM_API_KEY
                )
            elif self.config.LLM_PROVIDER == "openai":
                client = OpenAI(api_key=self.config.LLM_API_KEY)
            else:
                raise ValueError(f"Unknown LLM provider: {self.config.LLM_PROVIDER}")
            
            response = client.chat.completions.create(
                model=self.config.LLM_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=self.config.LLM_TEMPERATURE,
            )
            
            return response.choices[0].message.content.strip()
        
        except Exception as e:
            raise RuntimeError(f"Error calling LLM: {e}")
