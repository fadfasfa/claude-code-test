#!/usr/bin/env python3
"""
MCP (Model Context Protocol) Integration for Roo Code (Node C)

This module provides GitNexus MCP integration for the Hextech Nexus project.
It enables Roo Code to perform code analysis and context-aware auditing
during the final review phase.

Functions:
- analyze_code_context: Analyze code context using GitNexus
- assess_impact_radius: Assess impact radius of code changes
- generate_audit_report: Generate comprehensive audit report with MCP insights
"""

import subprocess
import json
import logging
from typing import Dict, List, Optional
from pathlib import Path

# Configure logging
logger = logging.getLogger(__name__)

class MCPIntegration:
    """MCP Integration class for Roo Code auditing."""

    def __init__(self, project_root: str = "."):
        """Initialize MCP integration with project root path."""
        self.project_root = Path(project_root).resolve()
        self.gitnexus_available = self._check_gitnexus()

    def _check_gitnexus(self) -> bool:
        """Check if GitNexus is available and working."""
        try:
            result = subprocess.run(
                ["gitnexus", "--version"],
                capture_output=True,
                text=True,
                timeout=10
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            logger.warning("GitNexus not available or not working properly")
            return False

    def analyze_code_context(self, target_file: str, symbol_name: Optional[str] = None) -> Dict:
        """
        Analyze code context using GitNexus context command.

        Args:
            target_file: Path to the target file to analyze
            symbol_name: Optional symbol name to get specific context for

        Returns:
            Dictionary containing context analysis results
        """
        if not self.gitnexus_available:
            logger.warning("GitNexus not available, skipping context analysis")
            return {"error": "GitNexus not available"}

        try:
            cmd = ["gitnexus", "context"]
            if symbol_name:
                cmd.extend(["-f", target_file, symbol_name])
            else:
                cmd.extend(["-f", target_file])

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=self.project_root
            )

            if result.returncode == 0:
                # Parse the output (assuming JSON format)
                try:
                    return json.loads(result.stdout)
                except json.JSONDecodeError:
                    # If not JSON, return as raw text
                    return {"raw_output": result.stdout}
            else:
                logger.error(f"GitNexus context command failed: {result.stderr}")
                return {"error": result.stderr}

        except Exception as e:
            logger.error(f"Error running GitNexus context analysis: {e}")
            return {"error": str(e)}

    def assess_impact_radius(self, target_file: str, target_symbol: str, direction: str = "upstream") -> Dict:
        """
        Assess impact radius using GitNexus impact command.

        Args:
            target_file: Path to the target file
            target_symbol: Symbol name to analyze impact for
            direction: Direction of impact analysis ("upstream" or "downstream")

        Returns:
            Dictionary containing impact analysis results
        """
        if not self.gitnexus_available:
            logger.warning("GitNexus not available, skipping impact analysis")
            return {"error": "GitNexus not available"}

        try:
            cmd = ["gitnexus", "impact", "-d", direction, "-f", target_file, target_symbol]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=self.project_root
            )

            if result.returncode == 0:
                try:
                    return json.loads(result.stdout)
                except json.JSONDecodeError:
                    return {"raw_output": result.stdout}
            else:
                logger.error(f"GitNexus impact command failed: {result.stderr}")
                return {"error": result.stderr}

        except Exception as e:
            logger.error(f"Error running GitNexus impact analysis: {e}")
            return {"error": str(e)}

    def generate_audit_report(self, target_files: List[str]) -> Dict:
        """
        Generate comprehensive audit report with MCP insights.

        Args:
            target_files: List of files to include in the audit

        Returns:
            Dictionary containing the complete audit report
        """
        report = {
            "mcp_enabled": self.gitnexus_available,
            "files_analyzed": [],
            "context_analysis": {},
            "impact_analysis": {},
            "recommendations": []
        }

        if not self.gitnexus_available:
            report["recommendations"].append("GitNexus not available - manual review recommended")
            return report

        for file_path in target_files:
            if not Path(file_path).exists():
                logger.warning(f"File not found: {file_path}")
                continue

            # Perform context analysis
            context_result = self.analyze_code_context(file_path)
            report["context_analysis"][file_path] = context_result

            # Perform impact analysis (if we can extract symbols)
            # This is a simplified version - in practice, you'd need to parse the file
            # to extract actual symbols to analyze
            impact_result = self.assess_impact_radius(file_path, "main", "upstream")
            report["impact_analysis"][file_path] = impact_result

            report["files_analyzed"].append(file_path)

        # Generate recommendations based on analysis
        self._generate_recommendations(report)

        return report

    def _generate_recommendations(self, report: Dict) -> None:
        """Generate recommendations based on MCP analysis results."""
        if not report["mcp_enabled"]:
            return

        # Add recommendations based on your specific needs
        report["recommendations"].append("MCP analysis completed successfully")
        report["recommendations"].append("Review context and impact analysis for each file")
        report["recommendations"].append("Pay special attention to high-impact changes")


def main():
    """Main function for testing MCP integration."""
    mcp = MCPIntegration()
    print(f"GitNexus available: {mcp.gitnexus_available}")

    if mcp.gitnexus_available:
        # Test with a sample file
        test_file = "run/web_server.py"
        if Path(test_file).exists():
            context = mcp.analyze_code_context(test_file)
            print(f"Context analysis for {test_file}:")
            print(json.dumps(context, indent=2))

            impact = mcp.assess_impact_radius(test_file, "app", "upstream")
            print(f"Impact analysis for {test_file}:")
            print(json.dumps(impact, indent=2))


if __name__ == "__main__":
    main()