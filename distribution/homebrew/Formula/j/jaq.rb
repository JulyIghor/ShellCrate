class Jaq < Formula
  desc "JQ clone focussed on correctness, speed, and simplicity"
  homepage "https://github.com/01mf02/jaq"
  url "https://github.com/01mf02/jaq/archive/refs/tags/v3.1.2.tar.gz"
  sha256 "54ac66fac30a1d28c54f19716215cf4256877c69964c1c0150c3976bde618b93"
  license "MIT"
  head "https://github.com/01mf02/jaq.git", branch: "main"

  livecheck do
    url :stable
    regex(/^v?(\d+(?:\.\d+)+)$/i)
  end

  depends_on "rust" => :build

  conflicts_with "json2tsv", because: "both install `jaq` binaries"

  deny_network_access!

  def fetch
    system "cargo", "fetch", *std_cargo_fetch_args
  end

  def install
    system "cargo", "install", *std_cargo_args(path: "jaq")
  end

  test do
    assert_match "1", pipe_output("#{bin}/jaq '.a'", '{"a": 1, "b": 2}', 0)
    assert_match "2.5", pipe_output("#{bin}/jaq -s 'add / length'", "1 2 3 4", 0)
  end
end
