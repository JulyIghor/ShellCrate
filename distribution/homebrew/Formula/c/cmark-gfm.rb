class CmarkGfm < Formula
  desc "C implementation of GitHub Flavored Markdown"
  homepage "https://github.com/github/cmark-gfm"
  url "https://github.com/github/cmark-gfm/archive/refs/tags/0.29.0.gfm.13.tar.gz"
  version "0.29.0.gfm.13"
  sha256 "5abc61798ebd9de5660bc076443c07abad2b8d15dbc11094a3a79644b8ad243a"
  license "BSD-2-Clause"

  depends_on "cmake" => :build
  uses_from_macos "python" => :build

  conflicts_with "cmark", because: "both install a `cmark.h` header"

  def install
    system "cmake", "-S", ".", "-B", "build",
                        "-DCMAKE_INSTALL_RPATH=#{rpath}",
                        *std_cmake_args
    system "cmake", "--build", "build"
    system "cmake", "--install", "build"
  end

  test do
    output = pipe_output("#{bin}/cmark-gfm --extension autolink", "https://brew.sh")
    assert_equal '<p><a href="https://brew.sh">https://brew.sh</a></p>', output.chomp
  end
end
