@echo off
title valset
python "%~dp0valset.py" %*
if "%~1"=="" pause
