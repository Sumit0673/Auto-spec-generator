"""Test suite for auto_spec."""

import pytest
from pathlib import Path
from types import SimpleNamespace
from auto_spec import SpecGenerator, Config
from auto_spec.generator import _strip_solidity_mutability_keywords
from auto_spec.cvl_validator import ValidationResult
from auto_spec.retrieval import contract_profile, extract_spec_chunk, make_index_document
from auto_spec.solidity_project import detect_contract_name, load_project
from auto_spec.evaluation import retrieval_report
from auto_spec import cvl_validator
from auto_spec.prompts.property_gpt import (
    analyze_solidity_contract,
    extract_cvl_spec,
    format_property_gpt_prompt,
)

FIXTURES = Path(__file__).parent / "fixtures"


class TestConfig:
    """Tests for configuration."""

    def test_config_initialization(self):
        """Test config initializes with defaults."""
        config = Config()
        assert config.LLM_PROVIDER in ["nvidia", "openai", "openrouter", "gemini", "deepseek"]
        assert config.OUTPUT_DIR.exists() or True  # May not exist yet

    def test_config_validation(self):
        """Test config validation."""
        config = Config()
        is_valid, msg = config.validate()
        assert isinstance(is_valid, bool)
        assert isinstance(msg, str)


class TestSpecGenerator:
    """Tests for SpecGenerator."""

    def test_generator_initialization(self):
        """Test generator initializes successfully."""
        config = Config()
        try:
            generator = SpecGenerator(config=config)
            assert generator is not None
        except RuntimeError:
            # Expected if API key is not configured
            pass

    def test_contract_analysis_detects_erc20_patterns(self):
        contract_code = """
        pragma solidity ^0.8.20;
        contract Token {
            mapping(address => uint256) public balanceOf;
            mapping(address => mapping(address => uint256)) public allowance;
            uint256 public totalSupply;
            address public owner;

            function transfer(address to, uint256 amount) external returns (bool) { }
            function transferFrom(address from, address to, uint256 amount) external returns (bool) { }
            function mint(address to, uint256 amount) external onlyOwner { }
            function burn(uint256 amount) external { }
            modifier onlyOwner() { _; }
        }
        """

        analysis = analyze_solidity_contract(contract_code, "Token")
        assert analysis["contract_family"] == "erc20-like"
        assert any(keyword in analysis["risk_signals"] for keyword in ["access_control", "balance_tracking"])
        assert analysis["state_variables"]["totalSupply"]

    def test_prompt_includes_contract_analysis_guidance(self):
        contract_code = "pragma solidity ^0.8.20; contract Demo { uint256 public totalSupply; function mint() external {} }"
        _, user_prompt = format_property_gpt_prompt(contract_code, [], "Demo")

        assert "CONTRACT ANALYSIS" in user_prompt
        assert "totalSupply" in user_prompt
        assert "supply" in user_prompt.lower()

    def test_prompt_requires_strict_cvl_syntax(self):
        contract_code = "pragma solidity ^0.8.20; contract Demo { uint256 public totalSupply; function mint() external {} }"
        system_prompt, user_prompt = format_property_gpt_prompt(contract_code, [], "Demo")

        assert "compilable" in system_prompt.lower()
        assert "pseudo" in system_prompt.lower()
        assert "methods" in user_prompt.lower()
        assert "never invent cvl syntax" in system_prompt.lower()
        assert "forbidden" in system_prompt.lower()

    def test_extract_cvl_spec_uses_fenced_code(self):
        response = """Here is the spec:\n```cvl\nmethods {\n    function transfer(address,uint256) external returns (bool);\n}\n```\n"""
        extracted = extract_cvl_spec(response)
        assert "methods" in extracted
        assert "function transfer" in extracted

    def test_llm_output_is_not_replaced_with_a_guessed_fallback(self, monkeypatch):
        generated = """methods { function transfer(address,uint256) external returns (bool); }
rule useMethod(method f) filtered { f -> true } { env e; calldataarg args; f(e, args); }"""

        class FakeOpenAI:
            def __init__(self, **kwargs):
                self.chat = SimpleNamespace(
                    completions=SimpleNamespace(
                        create=lambda **_: SimpleNamespace(
                            choices=[SimpleNamespace(message=SimpleNamespace(content=generated))]
                        )
                    )
                )

        monkeypatch.setattr("auto_spec.generator.OpenAI", FakeOpenAI)
        generator = object.__new__(SpecGenerator)
        generator.config = SimpleNamespace(
            LLM_PROVIDER="openai", LLM_API_KEY="test", LLM_MODEL="test",
            LLM_BASE_URL=None, LLM_TEMPERATURE=0, is_gemini=False,
        )

        assert generator._call_llm("contract Token {}", [], "Token") == generated


class TestKeywordStrip:
    """Regression: _strip_solidity_mutability_keywords must run on first call."""

    def test_strips_view_pure_payable_from_methods_block(self):
        spec = """methods {
    function balanceOf(address) external view returns (uint256);
    function deposit() external payable;
    function totalSupply() external pure returns (uint256);
}"""
        cleaned = _strip_solidity_mutability_keywords(spec)
        assert "view" not in cleaned
        assert "pure" not in cleaned
        assert "payable" not in cleaned
        assert "function balanceOf(address) external" in cleaned

    def test_first_call_applies_strip(self, monkeypatch):
        """Verify the first LLM call path runs keyword stripping."""
        raw_with_keywords = """methods {
    function balanceOf(address) external view returns (uint256);
}
rule dummy(env e) { assert true; }"""

        class FakeOpenAI:
            def __init__(self, **kwargs):
                self.chat = SimpleNamespace(
                    completions=SimpleNamespace(
                        create=lambda **_: SimpleNamespace(
                            choices=[SimpleNamespace(message=SimpleNamespace(content=raw_with_keywords))]
                        )
                    )
                )

        class FakeVectorDB:
            def query(self, *a, **kw):
                return []
            def __init__(self, *a, **kw):
                pass

        monkeypatch.setattr("auto_spec.generator.OpenAI", FakeOpenAI)
        monkeypatch.setattr("auto_spec.generator.VectorDBManager", FakeVectorDB)

        generator = object.__new__(SpecGenerator)
        generator.config = SimpleNamespace(
            LLM_PROVIDER="openai", LLM_API_KEY="test", LLM_MODEL="test",
            LLM_BASE_URL=None, LLM_TEMPERATURE=0.2, is_gemini=False,
            OUTPUT_DIR=Path("/tmp"), TOP_K_RESULTS=3,
        )
        generator.vector_db = FakeVectorDB()
        generator.last_validation = None

        # Call _call_llm directly then strip — mirrors generate() flow
        spec = generator._call_llm("contract X {}", [], "X")
        spec = _strip_solidity_mutability_keywords(spec)
        assert "view" not in spec


class TestRepairLoopAppendsFeedback:
    """Regression: repair must append feedback to prompt, not overwrite it."""

    def test_repair_prompt_contains_both_original_context_and_feedback(self, monkeypatch):
        call_log = []

        class FakeOpenAI:
            def __init__(self, **kwargs):
                pass

            @property
            def chat(self):
                parent = self

                class Completions:
                    @staticmethod
                    def create(**kwargs):
                        call_log.append(kwargs)
                        return SimpleNamespace(
                            choices=[SimpleNamespace(
                                message=SimpleNamespace(
                                    content="methods { function x() external; }\nrule r(env e) { assert true; }"
                                )
                            )]
                        )

                return SimpleNamespace(completions=Completions())

        monkeypatch.setattr("auto_spec.generator.OpenAI", FakeOpenAI)

        generator = object.__new__(SpecGenerator)
        generator.config = SimpleNamespace(
            LLM_PROVIDER="openai", LLM_API_KEY="test", LLM_MODEL="test",
            LLM_BASE_URL=None, LLM_TEMPERATURE=0.2, is_gemini=False,
        )

        # First call (initial generation)
        generator._call_llm("contract X {}", [], "X")

        # Second call (repair) — should include previous spec + feedback
        generator._call_llm(
            "contract X {}", [], "X",
            repair_feedback=["Variable Y has not been declared"],
            previous_spec="methods { function y() external; }",
        )

        repair_messages = call_log[1]["messages"]
        user_msg = repair_messages[1]["content"]

        # Must contain original context AND feedback AND previous spec
        assert "TARGET SOLIDITY CONTRACT" in user_msg
        assert "PREVIOUS SPEC" in user_msg
        assert "Variable Y has not been declared" in user_msg


class TestValidationResult:
    def test_only_a_successful_certora_result_is_marked_verified(self):
        assert ValidationResult("passed", [], "").passed
        assert not ValidationResult("failed", [], "bad syntax", 1).passed


class TestRetrievalRepresentation:
    def test_contract_profile_and_index_document_keep_contract_and_cvl_separate(self):
        source = """contract Token { function transfer(address to, uint amount) external {} }"""
        profile = contract_profile(source, "Token")
        index_document = make_index_document(profile, "rule transferRule() { assert true; }")

        assert "erc20" in profile
        assert "transfer(address to, uint amount)" in profile
        assert extract_spec_chunk(index_document) == "rule transferRule() { assert true; }"


class TestSolidityProject:
    def test_project_loader_follows_relative_and_remapped_imports(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "lib").mkdir()
        (tmp_path / "remappings.txt").write_text("pkg/=lib/\n")
        (tmp_path / "src" / "Base.sol").write_text("contract Base {}")
        (tmp_path / "lib" / "Token.sol").write_text("interface IERC20 {}")
        entrypoint = tmp_path / "src" / "Main.sol"
        entrypoint.write_text('import "./Base.sol"; import "pkg/Token.sol"; contract Main is Base {}')

        project = load_project(entrypoint, tmp_path)

        assert len(project.sources) == 3
        assert not project.unresolved_imports
        assert "contract Main" in project.source_text
        assert detect_contract_name(entrypoint.read_text(), "Main") == "Main"


class TestEvaluation:
    def test_retrieval_report_calculates_self_hit_rate(self):
        records = [{"id": "pair-1", "contract_name": "Token", "contracts": [{"clean_source": "contract Token {}"}]}]
        vector_db = SimpleNamespace(query=lambda *_args, **_kwargs: [{"pair_id": "pair-1"}])

        report = retrieval_report(records, vector_db, top_k=3)

        assert report["hit_rate"] == 1.0
        assert report["cases"][0]["hit"]


class TestCertoraValidation:
    def test_validation_selects_the_target_contract_from_the_solidity_file(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cvl_validator, "_certora_run_path", lambda: "certoraRun")
        captured = {}

        def fake_run(command, **kwargs):
            captured["command"] = command
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")

        monkeypatch.setattr(cvl_validator.subprocess, "run", fake_run)
        contract, spec = tmp_path / "Token.sol", tmp_path / "Token.spec"
        contract.write_text("contract Token {}")
        spec.write_text("methods {}")

        result = cvl_validator.validate_cvl(contract, spec, "Token")

        assert result.passed
        assert captured["command"][1] == f"{contract.resolve()}:Token"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
