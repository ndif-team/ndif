def test_pytest_working():
    """Basic test to verify pytest is working"""
    assert True, "Pytest is working!"

def test_imports():
    """Test that key dependencies are importable"""
    import pytest
    import ray
    import fastapi
    import requests
    import torch
    assert all([pytest, ray, fastapi, requests, torch]), "All required packages imported successfully"