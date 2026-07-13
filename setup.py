from setuptools import find_packages, setup

setup(
    name="kunjin",
    version="0.1.0",
    package_dir={"": "src"},
    packages=find_packages("src"),
    package_data={"kunjin": ["ledger/*.swift"]},
    include_package_data=True,
    install_requires=["cryptography>=43,<46"],
    entry_points={"console_scripts": ["kunjin=kunjin.cli:main"]},
)
