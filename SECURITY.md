# Security Policy

## Supported Versions

| Version | Supported          |
|---------|--------------------|
| 1.0.x   | :white_check_mark: |

## Reporting a Vulnerability

If you discover a security vulnerability in Oracle3, please report it responsibly.

**Do NOT open a public GitHub issue for security vulnerabilities.**

Instead, please email **yy85@illinois.edu** with:

1. A description of the vulnerability
2. Steps to reproduce the issue
3. Potential impact assessment
4. Any suggested fixes (optional)

You can expect an initial response within 48 hours. We will work with you to understand the issue and coordinate a fix before any public disclosure.

## Security Considerations

Oracle3 handles sensitive materials including private keys and API credentials. When using this software:

- **Never commit private keys** or `.env` files to version control
- **Use environment variables** for all secrets (see README for configuration)
- **Restrict file permissions** on keypair files (`chmod 600`)
- **Review transaction simulations** before enabling live trading
- **Start with paper trading** to validate strategies before committing real capital

## Scope

The following are in scope for security reports:

- Private key exposure or mishandling
- Transaction signing vulnerabilities
- API credential leakage
- Injection vulnerabilities in CLI or dashboard
- Unauthorized trade execution

The following are out of scope:

- Bugs in upstream dependencies (report to the respective project)
- Trading losses from strategy performance
- Issues requiring physical access to the host machine
