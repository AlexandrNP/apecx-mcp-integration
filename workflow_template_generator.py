#!/usr/bin/env python3
"""
Deterministic Workflow Template Generator for Nanobrain Adoption

This provides the systematic, reliable workflow generation machinery that
bypasses brittle LLM-dependent composition. Creates deterministic patterns
for epitope analysis workflows using validated APECX components.

Usage:
    python workflow_template_generator.py --pattern simple --virus covid
    python workflow_template_generator.py --pattern sophisticated --virus eeev
"""

import argparse
from pathlib import Path
from typing import Any

import yaml


class WorkflowTemplateGenerator:
    """Deterministic workflow template generation system."""

    def __init__(self):
        self.templates = {
            "simple": self._simple_template,
            "sophisticated": self._sophisticated_template,
            "minimal": self._minimal_template,
        }

        self.virus_configs = {
            "covid": {"full_name": "SARS-CoV-2", "protein": "spike protein", "complexity": "high"},
            "eeev": {
                "full_name": "Eastern Equine Encephalitis Virus",
                "protein": "envelope glycoprotein",
                "complexity": "high",
            },
            "rvfv": {
                "full_name": "Rift Valley Fever Virus",
                "protein": "glycoprotein Gn",
                "complexity": "medium",
            },
            "influenza": {
                "full_name": "Influenza A Virus",
                "protein": "hemagglutinin",
                "complexity": "high",
            },
        }

    def _simple_template(self, config: dict[str, Any]) -> dict[str, Any]:
        """Single-step RAG synthesis workflow."""
        return {
            "name": f"simple_{config['virus']}_epitope_analysis",
            "description": f"Basic epitope analysis for {config['full_name']} {config['protein']}",
            "version": "0.1.0",
            "config_version": 2,
            "steps": {
                "synthesis": {
                    "class": "apecx_integration.composition.steps.rag_synthesis_step.RagSynthesisStep",
                    "config": "steps/rag_synthesis.yml",
                }
            },
            "links": {
                "input_to_synthesis": {
                    "class": "nanobrain.core.link.DirectLink",
                    "config": {
                        "source": "workflow_input",
                        "target": "synthesis.synthesis_input",
                        "auto_transfer": True,
                    },
                },
                "synthesis_to_output": {
                    "class": "nanobrain.core.link.DirectLink",
                    "config": {
                        "source": "synthesis.synthesis_output",
                        "target": "workflow_output",
                        "auto_transfer": True,
                    },
                },
            },
            "triggers": [
                {
                    "class": "nanobrain.core.trigger.DataUnitChangeTrigger",
                    "config": {"data_unit": "workflow_input"},
                }
            ],
            "input_data_units": {
                "workflow_input": {
                    "class": "nanobrain.core.data_unit.DataUnitMemory",
                    "name": "workflow_input",
                    "persistent": False,
                }
            },
            "output_data_units": {
                "workflow_output": {
                    "class": "nanobrain.core.data_unit.DataUnitMemory",
                    "name": "workflow_output",
                    "persistent": False,
                }
            },
        }

    def _sophisticated_template(self, config: dict[str, Any]) -> dict[str, Any]:
        """Multi-source assembly + synthesis workflow."""
        return {
            "name": f"sophisticated_{config['virus']}_epitope_analysis",
            "description": f"Comprehensive multi-source epitope analysis for {config['full_name']} {config['protein']} with FAISS, VIOLIN/BV-BRC, and PubMed integration",
            "version": "0.1.0",
            "config_version": 2,
            "steps": {
                "assembly": {
                    "class": "apecx_integration.composition.steps.synthesis_context_assembly_step.SynthesisContextAssemblyStep",
                    "config": "steps/synthesis_context_assembly.yml",
                },
                "synthesis": {
                    "class": "apecx_integration.composition.steps.rag_synthesis_step.RagSynthesisStep",
                    "config": "steps/rag_synthesis.yml",
                },
            },
            "links": {
                "input_to_assembly": {
                    "class": "nanobrain.core.link.DirectLink",
                    "config": {
                        "source": "workflow_input",
                        "target": "assembly.assembly_input",
                        "auto_transfer": True,
                    },
                },
                "assembly_to_synthesis": {
                    "class": "nanobrain.core.link.DirectLink",
                    "config": {
                        "source": "assembly.synthesis_bundle_output",
                        "target": "synthesis.synthesis_input",
                        "auto_transfer": True,
                    },
                },
                "synthesis_to_output": {
                    "class": "nanobrain.core.link.DirectLink",
                    "config": {
                        "source": "synthesis.synthesis_output",
                        "target": "workflow_output",
                        "auto_transfer": True,
                    },
                },
            },
            "triggers": [
                {
                    "class": "nanobrain.core.trigger.DataUnitChangeTrigger",
                    "config": {"data_unit": "workflow_input"},
                }
            ],
            "input_data_units": {
                "workflow_input": {
                    "class": "nanobrain.core.data_unit.DataUnitMemory",
                    "name": "workflow_input",
                    "persistent": False,
                }
            },
            "output_data_units": {
                "workflow_output": {
                    "class": "nanobrain.core.data_unit.DataUnitMemory",
                    "name": "workflow_output",
                    "persistent": False,
                }
            },
        }

    def _minimal_template(self, config: dict[str, Any]) -> dict[str, Any]:
        """Bare-bones single-step workflow for testing."""
        return {
            "name": f"minimal_{config['virus']}_test",
            "description": f"Minimal test workflow for {config['full_name']}",
            "version": "0.1.0",
            "config_version": 2,
            "steps": {
                "test_step": {
                    "class": "apecx_integration.composition.steps.rag_synthesis_step.RagSynthesisStep",
                    "config": "steps/rag_synthesis.yml",
                }
            },
            "links": {
                "input_to_test": {
                    "class": "nanobrain.core.link.DirectLink",
                    "config": {
                        "source": "workflow_input",
                        "target": "test_step.synthesis_input",
                        "auto_transfer": True,
                    },
                },
                "test_to_output": {
                    "class": "nanobrain.core.link.DirectLink",
                    "config": {
                        "source": "test_step.synthesis_output",
                        "target": "workflow_output",
                        "auto_transfer": True,
                    },
                },
            },
            "triggers": [
                {
                    "class": "nanobrain.core.trigger.DataUnitChangeTrigger",
                    "config": {"data_unit": "workflow_input"},
                }
            ],
            "input_data_units": {
                "workflow_input": {
                    "class": "nanobrain.core.data_unit.DataUnitMemory",
                    "name": "workflow_input",
                    "persistent": False,
                }
            },
            "output_data_units": {
                "workflow_output": {
                    "class": "nanobrain.core.data_unit.DataUnitMemory",
                    "name": "workflow_output",
                    "persistent": False,
                }
            },
        }

    def generate_workflow(self, pattern: str, virus: str, output_file: str = None) -> Path:
        """Generate a workflow from template."""
        if pattern not in self.templates:
            raise ValueError(
                f"Unknown pattern: {pattern}. Available: {list(self.templates.keys())}"
            )

        if virus not in self.virus_configs:
            raise ValueError(
                f"Unknown virus: {virus}. Available: {list(self.virus_configs.keys())}"
            )

        # Merge config
        config = {"virus": virus, **self.virus_configs[virus]}

        # Generate workflow
        workflow_dict = self.templates[pattern](config)

        # Determine output file
        if output_file is None:
            output_file = f"generated_{workflow_dict['name']}.yml"

        output_path = Path(output_file)

        # Write YAML
        with open(output_path, "w") as f:
            yaml.dump(workflow_dict, f, default_flow_style=False, sort_keys=False)

        return output_path

    def generate_all_patterns(self, virus: str) -> list[Path]:
        """Generate all pattern types for a virus."""
        paths = []
        for pattern in self.templates:
            path = self.generate_workflow(pattern, virus)
            paths.append(path)
        return paths

    def list_available(self):
        """List available patterns and viruses."""
        print("Available patterns:")
        for pattern in self.templates:
            print(f"  - {pattern}")

        print("\nAvailable viruses:")
        for virus, config in self.virus_configs.items():
            print(f"  - {virus}: {config['full_name']} ({config['protein']})")


def main():
    parser = argparse.ArgumentParser(description="Generate deterministic nanobrain workflows")
    parser.add_argument(
        "--pattern",
        choices=["simple", "sophisticated", "minimal"],
        help="Workflow pattern to generate",
    )
    parser.add_argument(
        "--virus", choices=["covid", "eeev", "rvfv", "influenza"], help="Target virus for analysis"
    )
    parser.add_argument("--output", help="Output YAML file (default: auto-generated)")
    parser.add_argument(
        "--all-patterns", action="store_true", help="Generate all patterns for specified virus"
    )
    parser.add_argument("--list", action="store_true", help="List available patterns and viruses")

    args = parser.parse_args()

    generator = WorkflowTemplateGenerator()

    if args.list:
        generator.list_available()
        return

    if not args.virus:
        print("Error: --virus is required (unless using --list)")
        parser.print_help()
        return

    if args.all_patterns:
        paths = generator.generate_all_patterns(args.virus)
        print(f"Generated {len(paths)} workflows:")
        for path in paths:
            print(f"  - {path}")
    else:
        if not args.pattern:
            print("Error: --pattern is required (unless using --all-patterns)")
            parser.print_help()
            return

        path = generator.generate_workflow(args.pattern, args.virus, args.output)
        print(f"Generated workflow: {path}")


if __name__ == "__main__":
    main()
