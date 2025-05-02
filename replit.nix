{ pkgs }: {
    deps = [
        pkgs.python310Full
        pkgs.replitPackages.prybar-python310
        pkgs.replitPackages.stderred
        pkgs.postgresql
    ];
    env = {
        PYTHON_LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath [
            # 需要的系統庫
            pkgs.stdenv.cc.cc.lib
            pkgs.zlib
            pkgs.glib
            pkgs.libxml2
            pkgs.postgresql
        ];
        PYTHONHOME = "${pkgs.python310Full}";
        PYTHONBIN = "${pkgs.python310Full}/bin/python3.10";
        LANG = "en_US.UTF-8";
        STDERREDBIN = "${pkgs.replitPackages.stderred}/bin/stderred";
        PRYBAR_PYTHON_BIN = "${pkgs.replitPackages.prybar-python310}/bin/prybar-python310";
    };
} 