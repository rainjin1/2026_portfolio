from setuptools import find_packages, setup

package_name = 'qr_wall_scan'

setup(
    name=package_name,
    version='0.1.0',
    packages=find_packages(exclude=['test']),
    data_files=[
        ('share/ament_index/resource_index/packages',
            ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='Jin',
    maintainer_email='rainjin01@gmail.com',
    description='Outer wall perimeter QR scanning with Nav2',
    license='Apache-2.0',
    tests_require=['pytest'],
    entry_points={
        'console_scripts': [
            'qr_wall_scan_node = qr_wall_scan.qr_wall_scan_node:main',
        ],
    },
)
