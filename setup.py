"""Install Mesh TensorFlow."""

from setuptools import find_packages
from setuptools import setup

setup(
    name='mesh-tensorflow',
    version='0.0.5',
    description='Mesh TensorFlow',
    author='Google Inc.',
    author_email='no-reply@google.com',
    url='http://github.com/tensorflow/mesh',
    license='Apache 2.0',
    packages=find_packages(),
    package_data={
        # Include gin files.
        '': ['*.gin'],
    },
    scripts=[],
    install_requires=[
        'future',
        'gin-config',
        'six',
    ],
    extras_require={
        'auto_mtf': ['ortools>=7.0.6546'],
        'tensorflow': ['tensorflow>=1.9.0'],
        'tensorflow_gpu': ['tensorflow-gpu>=1.9.0'],
        'tests': [
            'absl-py',
            'pytest',
            'ortools>=7.0.6546',
            'tensor2tensor>=1.9.0',  # TODO(trandustin): rm dependence
        ],
    },
    classifiers=[
        'Development Status :: 4 - Beta',
        'Intended Audience :: Developers',
        'Intended Audience :: Science/Research',
        'License :: OSI Approved :: Apache Software License',
        'Topic :: Scientific/Engineering :: Artificial Intelligence',
    ],
    keywords='tensorflow machine learning',
)
