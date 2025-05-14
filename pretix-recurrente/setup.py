import os
from setuptools import setup, find_packages

# Lee la versión del código
try:
    with open(os.path.join(os.path.dirname(__file__), 'pretix_recurrente', '__init__.py'), 'r') as f:
        for line in f:
            if line.startswith('__version__'):
                version = line.split('=')[1].strip().strip("'")
                break
except:
    version = '0.1.0'

# Lee el README.md
try:
    with open(os.path.join(os.path.dirname(__file__), 'README.md'), 'r', encoding='utf-8') as f:
        long_description = f.read()
except:
    long_description = ''

setup(
    name="pretix-recurrente",
    version=version,
    description="Pasarela de pago Recurrente para Pretix",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/dentrada/pretix-recurrente",
    author="Dentrada",
    author_email="info@dentrada.com",
    license="Apache Software License",
    install_requires=[
        "requests>=2.25.1",
        "svix>=1.8.0",
    ],
    packages=find_packages(),
    include_package_data=True,
    entry_points="""
[pretix.plugin]
pretix_recurrente=pretix_recurrente:PluginApp
""",
    project_urls={
        "Bug Reports": "https://github.com/dentrada/pretix-recurrente/issues",
        "Source": "https://github.com/dentrada/pretix-recurrente",
    },
    classifiers=[
        'Development Status :: 4 - Beta',
        'Environment :: Plugins',
        'Intended Audience :: Developers',
        'Intended Audience :: Other Audience',
        'License :: OSI Approved :: Apache Software License',
        'Natural Language :: Spanish',
        'Operating System :: OS Independent',
        'Programming Language :: Python :: 3.8',
        'Programming Language :: Python :: 3.9',
        'Programming Language :: Python :: 3.10',
        'Programming Language :: Python :: 3.11',
        'Framework :: Django :: 4.2',
        'Topic :: Internet :: WWW/HTTP :: Dynamic Content',
        'Topic :: Software Development :: Libraries :: Python Modules',
    ],
    python_requires='>=3.8',
)
