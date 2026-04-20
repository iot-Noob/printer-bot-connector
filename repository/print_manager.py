import subprocess
import os
import re
import shlex
import asyncio
import logging
from pathlib import Path
from typing import List, Optional, Dict
from .config import config

logger = logging.getLogger(__name__)

class PrintManager:
    def __init__(self):
        self.max_copies = config.max_copies

    async def get_available_printers(self) -> List[str]:
        """Fetch available printers asynchronously."""
        try:
            if os.name == 'nt':
                # Windows - Using array arguments to avoid injection
                cmd = ['wmic', 'printer', 'get', 'name']
            else:
                # Linux - Using standard lpstat
                cmd = ['lpstat', '-e']

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            
            if process.returncode != 0:
                logger.error(f"Failed to get printers: {stderr.decode()}")
                return []

            output = stdout.decode().strip()
            if os.name == 'nt':
                return [p.strip() for p in output.split('\n')[1:] if p.strip()]
            else:
                return [p.strip() for p in output.split('\n') if p.strip()]
        except Exception as e:
            logger.error(f"Printer discovery error: {e}")
            return ["Default Printer"]

    async def get_print_queue(self) -> List[str]:
        """Fetch current print queue with structured IDs."""
        try:
            if os.name == 'nt':
                # Windows - Get JobID and Document name
                cmd = ['wmic', 'printjob', 'get', 'jobid,document']
            else:
                # Linux - Get basic job info
                cmd = ['lpstat', '-o']

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await process.communicate()
            output = stdout.decode().strip()
            
            if not output:
                return []

            jobs = []
            lines = [l.strip() for l in output.split('\n') if l.strip()]
            
            if os.name == 'nt':
                # Skip header 'Document  JobId'
                for line in lines[1:]:
                    # wmic output is often fixed-width or space-separated
                    parts = line.split()
                    if len(parts) >= 2:
                        job_id = parts[-1]
                        doc = " ".join(parts[:-1])
                        jobs.append(f"#{job_id}: {doc}")
            else:
                for line in lines:
                    # Linux format: 'printer-123 user size ...'
                    parts = line.split()
                    if parts:
                        job_id = parts[0]
                        jobs.append(f"{job_id}")
            
            return jobs
        except Exception as e:
            logger.error(f"Queue fetch error: {e}")
            return []

    async def cancel_print_job(self, job_id: str) -> bool:
        """Cancel a print job by ID."""
        try:
            if os.name == 'nt':
                # Sanitize: job_id should be numeric
                numeric_id = "".join(filter(str.isdigit, job_id))
                if not numeric_id:
                    return False
                # Use wmic to delete the job
                cmd = ['wmic', 'printjob', 'where', f'jobid={numeric_id}', 'delete']
            else:
                # Linux - Use cancel command
                # job_id is usually 'printer-123'
                cmd = ['cancel', job_id]

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
            
            if process.returncode == 0:
                logger.info(f"Cancelled job: {job_id}")
                return True
            else:
                logger.error(f"Failed to cancel job {job_id}: {stderr.decode()}")
                return False
        except asyncio.TimeoutError:
            logger.error(f"Timeout cancelling job {job_id}")
            return False
        except Exception as e:
            logger.error(f"Cancel error: {e}")
            return False
    async def resume_print_job(self, job_id: str) -> bool:
        """Force resume a print job by ID."""
        try:
            if os.name == 'nt':
                # Sanitize: job_id should be numeric for wmic
                numeric_id = "".join(filter(str.isdigit, job_id))
                if not numeric_id:
                    return False
                # Use wmic to resume the job
                cmd = ['wmic', 'printjob', 'where', f'jobid={numeric_id}', 'call', 'resume']
            else:
                # Linux - Use lp command with -H resume
                cmd = ['lp', '-i', job_id, '-H', 'resume']

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
            
            if process.returncode == 0:
                logger.info(f"Resumed job: {job_id}")
                return True
            else:
                logger.error(f"Failed to resume job {job_id}: {stderr.decode()}")
                return False
        except asyncio.TimeoutError:
            logger.error(f"Timeout resuming job {job_id}")
            return False
        except Exception as e:
            logger.error(f"Resume error: {e}")
            return False

    async def convert_to_pdf(self, file_path: str) -> Optional[str]:
        """Convert document to PDF using LibreOffice headlessly."""
        try:
            file_path_obj = Path(file_path).resolve()
            output_dir = str(file_path_obj.parent)
            output_pdf = str(file_path_obj.with_suffix('.pdf'))
            
            # SAFE: No shell=True, direct argument list
            cmd = [
                'libreoffice', '--headless', '--convert-to', 'pdf',
                '--outdir', output_dir, str(file_path_obj)
            ]
            
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=120)
            
            if process.returncode == 0 and os.path.exists(output_pdf):
                return output_pdf
            else:
                logger.error(f"Conversion failed: {stderr.decode()}")
                return None
        except asyncio.TimeoutError:
            logger.error("PDF conversion timed out after 120s")
            return None
        except Exception as e:
            logger.error(f"Conversion error: {e}")
            return None

    async def print_file(self, file_path: str, settings: Dict, page_range: Optional[str] = None) -> str:
        """Execute print command with security hardening."""
        try:
            # 1. Path Security: Sanitize and resolve
            path_obj = Path(file_path).resolve()
            if not path_obj.exists():
                return f"❌ File not found: {path_obj.name}"

            copies = min(settings.get("copies", 1), self.max_copies)
            selected_printer = settings.get("printer")
            
            # 2. Windows Hardening: Skip PowerShell string interpolation
            # Use 'cmd /c start' or direct call if possible.
            # 'start /min' is safer for handling paths with spaces via the start command logic.
            if os.name == 'nt':
                # Modern Windows approach: Handle specific printer selection
                # Note: Start-Process -Verb Print uses the default printer.
                # To honor 'selected_printer', we temporarily set it as default via CIM.
                escaped_path = str(path_obj).replace("'", "''")
                
                if selected_printer:
                    escaped_printer = selected_printer.replace("'", "''")
                    # Fuzzy match: match exact OR partial name to handle "HP LaserJet" vs "Hewlett-Packard..."
                    ps_command = (
                        f"$n = '{escaped_printer}'; "
                        f"$p = Get-CimInstance Win32_Printer | Where-Object {{ $_.Name -eq $n -or $_.Name -like \"*$n*\" }}; "
                        f"if ($p -is [array]) {{ $p = $p[0] }}; "
                        f"if ($p) {{ "
                        f"  $p | Invoke-CimMethod -MethodName SetDefaultPrinter; "
                        f"  Write-Output \"MATCHED_PRINTER: $($p.Name)\" "
                        f"}}; "
                        f"Start-Process -FilePath '{escaped_path}' -Verb Print -WindowStyle Hidden"
                    )
                else:
                    ps_command = f"Start-Process -FilePath '{escaped_path}' -Verb Print -WindowStyle Hidden"

                cmd = [
                    'powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                    '-Command', ps_command
                ]
                
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
                
                output = stdout.decode()
                if "MATCHED_PRINTER:" in output:
                    logger.info(f"Windows Print Success: {output.strip()}")
                
                if process.returncode != 0:
                    return f"❌ Windows Print failed: {stderr.decode()}"
            
            # 3. Linux Hardening: LP with direct args
            else:
                cmd = ['lp']
                if selected_printer:
                    cmd.extend(['-d', selected_printer])
                if copies > 1:
                    cmd.extend(['-n', str(copies)])
                if page_range and re.match(r'^\d+-\d+$', page_range):
                    cmd.extend(['-o', f'page-ranges={page_range}'])
                
                cmd.append(str(path_obj))
                
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
                
                if process.returncode != 0:
                    return f"❌ Print failed: {stderr.decode()}"
            
            printer_text = f" to {selected_printer}" if selected_printer else ""
            copy_text = f" ({copies} copies)" if copies > 1 else ""
            return f"✅ Printed: {path_obj.name}{copy_text}{printer_text}"

        except asyncio.TimeoutError:
            return f"❌ Print timeout for {os.path.basename(file_path)}"
        except Exception as e:
            logger.error(f"Print error: {e}")
            return f"❌ Print error: {e}"

# Singleton
print_manager = PrintManager()
